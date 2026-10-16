"""Offline security/state-machine checks. No native store or real HTTP is touched."""
from vlc_monitor.notifications.credentials import CredentialError
from vlc_monitor.notifications.wechat import BASE_URL, KEY, WechatBindingController, retry_after_seconds, validated_origin
import pytest


class MemoryStore:
    def __init__(self):
        self.data = {}
        self.fail_save = self.fail_delete = False
        self.reads = 0

    def load(self, key):
        self.reads += 1
        return self.data.get(key)

    def save(self, key, value):
        if self.fail_save:
            raise CredentialError("Fake write failure")
        self.data[key] = value

    def delete(self, key):
        if self.fail_delete:
            raise CredentialError("Fake delete failure")
        self.data.pop(key, None)


class FakeTransport:
    def __init__(self):
        self.calls = []
        self.cancellations = 0
        self.requests = []

    def request(self, method, origin, endpoint, payload, token, callback, timeout=15):
        self.calls.append((endpoint, callback))
        self.requests.append((method, origin, endpoint, payload, token, timeout))

    def answer(self, payload=None, error=None):
        self.calls[-1][1](payload, error)

    def cancel(self):
        self.cancellations += 1


def setup():
    store, transport, now = MemoryStore(), FakeTransport(), [0.0]
    return WechatBindingController(store, transport, clock=lambda: now[0]), store, transport, now


def login(controller, transport):
    controller.enable()
    controller.start_binding()
    transport.answer({"qrcode": "fake-qr", "qrcode_img_content": "fake-payload"})
    controller.tick()
    transport.answer({"status": "confirmed", "bot_token": "fake-token", "ilink_bot_id": "fake-bot", "ilink_user_id": "fake-owner", "baseurl": BASE_URL})


def message(controller, **changes):
    result = {"message_type": 1, "from_user_id": "fake-owner", "to_user_id": "fake-bot", "context_token": "fake-context", "item_list": [{"type": 1, "text_item": {"text": controller.challenge}}]}
    result.update(changes)
    return result


def bind(controller, transport):
    login(controller, transport)
    controller.tick()
    transport.answer({"msgs": [message(controller)]})
    controller.confirm_candidate()


def test_default_disabled_is_zero_network_and_zero_credential_reads():
    controller, store, transport, _ = setup()
    controller.tick()
    controller.start_binding()
    controller.send_test()
    assert controller.state == "disabled"
    assert not transport.calls and store.reads == 0
    controller.enable()
    assert controller.state == "unbound" and not transport.calls


def test_challenge_checks_owner_bot_expiry_and_requires_manual_confirmation():
    controller, store, transport, _ = setup()
    login(controller, transport)
    for overrides in ({"from_user_id": "other-user"}, {"to_user_id": "other-bot"}, {"item_list": [{"type": 1, "text_item": {"text": "unrelated"}}]}):
        controller.tick()
        transport.answer({"msgs": [message(controller, **overrides)]})
        assert controller.state == "waiting_challenge" and not store.data
    controller.tick()
    transport.answer({"msgs": [message(controller)]})
    assert controller.state == "waiting_confirmation" and not store.data
    assert not controller.challenge
    controller.confirm_candidate()
    assert controller.state == "context_unverified" and KEY in store.data
    controller.confirm_candidate()
    controller.send_test()
    transport.answer({"ret": 0})
    assert controller.test_accepted and not controller.phone_confirmed
    controller.confirm_phone_receipt()
    assert controller.phone_confirmed and controller.snapshot()["real_alarms_available"]


def test_expiration_and_late_login_cannot_bind():
    controller, store, transport, now = setup()
    controller.enable()
    controller.start_binding()
    now[0] = 181
    transport.answer({"qrcode": "fake", "qrcode_img_content": "fake"})
    assert controller.state == "expired" and not store.data
    login(controller, transport)
    controller.tick()
    pending = transport.calls[-1][1]
    event = message(controller)
    now[0] += 181
    controller.tick()
    pending({"msgs": [event]}, None)
    assert controller.state == "expired" and controller.candidate is None


def test_credential_write_failure_never_becomes_sendable():
    controller, store, transport, _ = setup()
    store.fail_save = True
    bind(controller, transport)
    assert controller.state == "credential_unavailable" and not controller.enabled
    assert controller.binding is None and not store.data
    before = len(transport.calls)
    controller.send_test()
    assert len(transport.calls) == before


def test_unbind_failure_keeps_disabled_and_rejects_late_results():
    controller, store, transport, _ = setup()
    bind(controller, transport)
    controller.send_test()
    pending = transport.calls[-1][1]
    old_generation = controller.generation
    store.fail_delete = True
    controller.unbind()
    pending({"ret": 0}, None)
    controller.enable()
    assert not controller.enabled and controller.deletion_failed
    assert controller.generation > old_generation and not controller.test_accepted
    assert controller.result == "credential_delete_failed"
    store.fail_delete = False
    controller.unbind()
    assert not store.data and not controller.deletion_failed


def test_disable_restore_never_resends_or_trusts_old_test():
    controller, store, transport, _ = setup()
    bind(controller, transport)
    controller.send_test()
    pending = transport.calls[-1][1]
    controller.disable()
    controller.enable()
    pending({"ret": 0}, None)
    assert controller.state == "context_unverified" and not controller.test_accepted
    before = len(transport.calls)
    controller.tick()
    assert len(transport.calls) == before and KEY in store.data


@pytest.mark.parametrize("origin", ["http://ilinkai.weixin.qq.com", "https://ilinkai.weixin.qq.com.evil.test", "https://user@ilinkai.weixin.qq.com", "https://ilinkai.weixin.qq.com:444", "https://other.weixin.qq.com", BASE_URL + "/redirect"])
def test_origin_allowlist_is_exact_and_fail_closed(origin):
    with pytest.raises(ValueError):
        validated_origin(origin)


def test_unsupported_login_branch_and_invalid_session_stop_without_relogin():
    controller, store, transport, _ = setup()
    controller.enable()
    controller.start_binding()
    transport.answer({"qrcode": "fake", "qrcode_img_content": "fake"})
    controller.tick()
    transport.answer({"status": "need_verifycode"})
    assert controller.state == "blocked" and not controller.qr_id
    before = len(transport.calls)
    controller.tick()
    assert len(transport.calls) == before
    controller.start_binding()
    transport.answer(None, "session_invalid")
    controller.tick()
    assert controller.state == "session_invalid"


def test_failed_native_cleanup_is_blocked_across_controller_restart(tmp_path):
    from vlc_monitor.notifications.credentials import GuardedCredentialStore
    native = MemoryStore()
    marker = tmp_path / "cleanup-required"
    guarded = GuardedCredentialStore(lambda: native, marker)
    controller = WechatBindingController(guarded, FakeTransport())
    bind(controller, controller.transport)
    native.fail_delete = True
    controller.unbind()
    assert marker.exists()
    new_store = GuardedCredentialStore(lambda: native, marker)
    restarted = WechatBindingController(new_store, FakeTransport())
    restarted.enable()
    assert not restarted.enabled and restarted.binding is None
    native.fail_delete = False
    restarted.unbind()
    assert not marker.exists() and not native.data


@pytest.mark.parametrize("payload", [{"ret": 0}, {"errcode": 0}, {"ret": 0, "errcode": 0}])
def test_alarm_explicit_acceptance_and_wire_schema(payload):
    controller, _, transport, _ = setup()
    bind(controller, transport)
    results = []
    assert controller.snapshot()["real_alarms_available"]
    controller.send_alarm("fake alarm", "stable-id", lambda *args: results.append(args))
    assert controller.busy
    request = transport.requests[-1]
    assert request[:3] == ("POST", BASE_URL, "/ilink/bot/sendmessage")
    assert request[3] == {"msg": {"from_user_id": "", "to_user_id": "fake-owner", "client_id": "stable-id",
                                "message_type": 2, "message_state": 2, "context_token": "fake-context",
                                "item_list": [{"type": 1, "text_item": {"text": "fake alarm"}}]},
                          "base_info": {"channel_version": "0.1.0", "bot_agent": "VLCMonitor/0.1.0"}}
    transport.answer(payload)
    assert results == [("accepted", None)] and not controller.busy


@pytest.mark.parametrize("payload", [{}, {"ret": 1}, {"ret": 0, "errcode": 1}, {"ret": False}, {"ret": "0"}, None])
def test_alarm_rejects_missing_or_conflicting_success(payload):
    controller, _, transport, _ = setup()
    bind(controller, transport)
    results = []
    controller.send_alarm("fake", "id", lambda *args: results.append(args))
    transport.answer(payload)
    assert results == [("failed", None)]


@pytest.mark.parametrize("error,expected", [("uncertain", "uncertain"), ("api_rejected", "failed"),
                                            ("network_failed", "failed"), ("secret raw server error", "failed")])
def test_alarm_error_results_are_fixed_and_sanitized(error, expected):
    controller, _, transport, _ = setup()
    bind(controller, transport)
    results = []
    controller.send_alarm("fake", "id", lambda *args: results.append(args))
    transport.answer(None, error)
    assert results == [(expected, None)]
    assert controller.result == "alarm_" + expected


def test_alarm_session_invalid_pauses_without_automatic_reenable():
    controller, _, transport, _ = setup()
    bind(controller, transport)
    results = []
    controller.send_alarm("fake", "id", lambda *args: results.append(args))
    transport.answer({"ret": -14})
    before = len(transport.calls)
    controller.enable()
    controller.tick()
    controller.send_test()
    controller.send_alarm("fake", "id", lambda *args: results.append(args))
    assert len(transport.calls) == before
    assert not controller.snapshot()["real_alarms_available"]
    assert results == [("session_invalid", None)] * 2


def test_alarm_reports_completion_before_session_invalid_snapshot():
    controller, _, transport, _ = setup()
    bind(controller, transport)
    events = []
    controller.changed = lambda snapshot: events.append(("snapshot", snapshot["state"]))
    controller.send_alarm("fake", "id", lambda result, _: events.append(("callback", result, controller.busy)))
    events.clear()
    transport.answer({"ret": -14})
    assert events == [("callback", "session_invalid", False), ("snapshot", "session_invalid")]


def test_alarm_epoch_and_generation_discard_late_callbacks():
    controller, _, transport, _ = setup()
    bind(controller, transport)
    results = []
    controller.send_alarm("fake", "id", lambda *args: results.append(args))
    pending = transport.calls[-1][1]
    controller.disable()
    controller.enable()
    controller.send_alarm("new", "new-id", lambda *args: results.append(args))
    pending({"ret": 0}, None)
    assert controller.busy and not results
    controller.send_epoch += 1
    transport.answer({"ret": 0})
    assert not results


def test_alarm_shared_busy_gate_and_default_off():
    controller, _, transport, _ = setup()
    results = []
    callback = lambda *args: results.append(args)
    controller.send_alarm("fake", "id", callback)
    assert not transport.calls and results == [("failed", None)]
    bind(controller, transport)
    controller.send_alarm("fake", "id", callback)
    before = len(transport.calls)
    controller.send_test()
    controller.tick()
    controller.start_binding()
    controller.send_alarm("another", "id2", callback)
    assert len(transport.calls) == before and results[-1] == ("transient", None)


@pytest.mark.parametrize("metadata,expected", [(5, 5.0), ("12", 12.0), (float("inf"), None), ("raw text", None)])
def test_alarm_retry_metadata_is_numeric_only(metadata, expected):
    controller, _, transport, _ = setup()
    bind(controller, transport)
    results = []
    controller.send_alarm("fake", "id", lambda *args: results.append(args))
    transport.answer({"retry_after": metadata, "secret": "never expose"}, "transient")
    assert results == [("transient", expected)]


def test_retry_after_supports_seconds_and_http_date():
    assert retry_after_seconds("10") == 10
    assert retry_after_seconds("Wed, 21 Oct 2015 07:28:00 GMT", now=1445412470) == 10
    assert retry_after_seconds("Wed, 21 Oct 2015 07:28:00 GMT", now=1445412490) == 0
    assert retry_after_seconds("NaN") is None


def test_cancel_alarm_preserves_epoch_and_never_cancels_manual_test():
    controller, _, transport, _ = setup()
    bind(controller, transport)
    controller.send_test()
    controller.cancel_alarm()
    assert controller.busy and transport.cancellations == 0
    transport.answer({"ret": 0})
    results = []
    epoch = controller.send_epoch
    assert controller.send_alarm("fake", "id", lambda *args: results.append(args)) is True
    pending = transport.calls[-1][1]
    controller.cancel_alarm()
    assert not controller.busy and transport.cancellations == 1 and controller.send_epoch == epoch
    controller.send_alarm("new", "new-id", lambda *args: results.append(args))
    pending({"ret": 0}, None)
    assert controller.busy and not results
    transport.answer({"ret": 0})
    assert results == [("accepted", None)]


@pytest.mark.parametrize("status,network_error,body,header,expected,metadata", [
    (429, False, b"private", b"20", "transient", {"retry_after": 20.0}),
    (503, False, b"private", b"invalid", "transient", None),
    (401, False, b"private", b"", "session_invalid", None),
    (None, True, b"", b"", "uncertain", None),
    (200, False, b'{"ret":1,"private":"hidden"}', b"", "api_rejected", None),
    (200, False, b'{}', b"", "api_rejected", None),
    (200, False, b'{"ret":0}', b"", None, {"ret": 0}),
])
def test_qt_transport_classifies_send_without_network(monkeypatch, status, network_error, body, header, expected, metadata):
    import os
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6 import QtCore, QtNetwork
    from PySide6.QtWidgets import QApplication
    from vlc_monitor.notifications.wechat import QtJsonTransport

    app = QApplication.instance() or QApplication([])

    class Reply(QtCore.QObject):
        requestSent = QtCore.Signal()
        readyRead = QtCore.Signal()
        finished = QtCore.Signal()

        def readAll(self):
            return QtCore.QByteArray(body)

        def attribute(self, _):
            return status

        def error(self):
            return QtNetwork.QNetworkReply.NetworkError.TimeoutError if network_error else QtNetwork.QNetworkReply.NetworkError.NoError

        def rawHeader(self, _):
            return QtCore.QByteArray(header)

        def abort(self):
            self.finished.emit()

    reply = Reply()

    class Manager:
        def __init__(self, _):
            pass

        def post(self, *_):
            return reply

    monkeypatch.setattr(QtNetwork, "QNetworkAccessManager", Manager)
    transport = QtJsonTransport()
    results = []
    transport.request("POST", BASE_URL, "/ilink/bot/sendmessage", {}, "fake-token", lambda *args: results.append(args))
    reply.finished.emit()
    assert results == [(metadata, expected)]
    assert not transport.pending
    assert app is not None
