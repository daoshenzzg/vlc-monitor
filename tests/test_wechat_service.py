"""Offline actor/lifecycle checks; never open the OS store or network."""
import json
import os
import time
from dataclasses import replace
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
import pytest
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication

from vlc_monitor.models import AlarmEvent, RegionConfig
from vlc_monitor.notifications.coordinator import AcceptedEvent
from vlc_monitor.notifications.wechat import BASE_URL
from vlc_monitor.notifications.wechat_service import WechatNotificationService


def pump(predicate, timeout=3):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        QApplication.processEvents()
        if predicate():
            return
        time.sleep(.005)
    assert predicate()


@pytest.fixture
def service(tmp_path):
    app = QApplication.instance() or QApplication([])
    facts = SimpleNamespace(reads=0, calls=[], mode="accepted", callbacks=[])

    class Store:
        def load(self, key):
            facts.reads += 1
            return json.dumps(dict(token="fake-token", account="fake-bot", owner="fake-owner",
                recipient="fake-owner", context="fake-context", reference="fake-reference", origin=BASE_URL)).encode()

    class Transport:
        def request(self, method, origin, endpoint, payload, token, callback, timeout=15):
            facts.calls.append(payload["msg"]["item_list"][0]["text_item"]["text"])
            if facts.mode == "accepted":
                QTimer.singleShot(1, lambda: callback({"ret": 0}, None))
            elif facts.mode == "invalid":
                QTimer.singleShot(1, lambda: callback(None, "session_invalid"))
            else:
                facts.callbacks.append(callback)

        def cancel(self):
            for callback in facts.callbacks:
                callback({"ret": 0}, None)  # An abort may race a late server success.
            facts.callbacks.clear()

    value = WechatNotificationService(tmp_path, factory=lambda parent: (Store(), Transport()))
    reports = []
    value.delivery.connect(reports.append)
    pump(lambda: hasattr(value.worker, "timer") and value.worker.timer.isActive())
    # Production constants remain unchanged; only this fake actor uses a short aggregate.
    value.worker.queue.AGGREGATE = .01
    value.set_context("s", True, [RegionConfig(id="r")], run_id=1)
    yield value, facts, reports
    value.shutdown()
    pump(lambda: not value.thread.isRunning())
    app.processEvents()


def alarm(event_id="a"):
    now = time.time()
    return AlarmEvent(event_id, "s", "r", "区域", "black", 0, now, now, now, 4, run_id=1)


def enable(service):
    service.command("enable")
    pump(lambda: service.snapshot["real_alarms_available"])


def test_default_off_zero_reads_network_and_no_history_catchup(service):
    value, facts, _ = service
    value.publish([AcceptedEvent(alarm(), "start")])
    QApplication.processEvents()
    assert facts.reads == 0 and not facts.calls
    enable(value)
    value.publish([AcceptedEvent(alarm(), "update")])
    value.publish([AcceptedEvent(replace(alarm(), ended_at=time.time(), end_reason="恢复正常"), "end")])
    pump(lambda: not value._inbox.scheduled)
    assert not facts.calls


def test_ordered_initial_recovery_and_main_thread_delivery_results(service):
    value, facts, reports = service
    enable(value)
    value.publish([AcceptedEvent(alarm(), "start")])
    pump(lambda: any(r["result"] == "accepted" for r in reports))
    value.publish([AcceptedEvent(replace(alarm(), ended_at=time.time(), end_reason="恢复正常"), "end")])
    pump(lambda: len(reports) == 2)
    assert [r["phase"] for r in reports] == ["start", "recovery"]
    assert "首次告警" in facts.calls[0] and "恢复正常" in facts.calls[1]


def test_context_cancels_inflight_and_late_success_cannot_reopen(service):
    value, facts, reports = service
    facts.mode = "pending"
    enable(value)
    value.publish([AcceptedEvent(alarm(), "start")])
    pump(lambda: bool(facts.calls))
    value.set_context("new", False, [])
    pump(lambda: any(r["result"] == "cancelled" for r in reports))
    assert not any(r["result"] == "accepted" for r in reports)
    assert not value.worker.controller.busy


def test_session_invalid_records_failure_and_pauses_channel(service):
    value, facts, reports = service
    facts.mode = "invalid"
    enable(value)
    value.publish([AcceptedEvent(alarm(), "start")])
    pump(lambda: value.snapshot["state"] == "session_invalid")
    assert [r["result"] for r in reports] == ["failed"]
    assert not value.snapshot["real_alarms_available"]


def test_quiesce_keeps_actor_reusable_after_failed_player_shutdown(service):
    value, facts, reports = service
    enable(value)
    pump(lambda: not value._inbox.scheduled)
    value._wake.disconnect(value.worker.drain)
    generation, epoch = value.snapshot["generation"], value.snapshot["send_epoch"]
    pending = [AcceptedEvent(alarm(), "start"),
               AcceptedEvent(replace(alarm(), ended_at=time.time(), end_reason="恢复正常"), "end")]
    value.publish(pending)
    value.publish(pending)  # Each mailbox phase remains one accepted task.
    value.quiesce()
    assert reports == [
        {"event_ids": ("a",), "phase": phase, "generation": generation,
         "send_epoch": epoch, "sequence": 0, "attempts": 0, "result": "cancelled"}
        for phase in ("start", "recovery")]
    assert not facts.calls
    value._wake.connect(value.worker.drain)
    value._wake.emit()
    pump(lambda: value.snapshot["state"] == "disabled")
    assert value.thread.isRunning()
    enable(value)
    value.publish([AcceptedEvent(alarm("new"), "start")])
    pump(lambda: any(r["result"] == "accepted" for r in reports))
    assert reports[-1]["result"] == "accepted"
    assert len(reports) == 3
    value._wake.disconnect(value.worker.drain)
    value.publish([AcceptedEvent(alarm("last"), "start")])
    generation, epoch = value.snapshot["generation"], value.snapshot["send_epoch"]
    value.shutdown()
    value.shutdown()
    pump(lambda: not value.thread.isRunning())
    assert len(reports) == 4
    assert reports[-1] == {"event_ids": ("last",), "phase": "start", "generation": generation,
                          "send_epoch": epoch, "sequence": 0, "attempts": 0, "result": "cancelled"}


def test_manual_test_has_separate_sanitized_delivery_record(service):
    value, facts, reports = service
    enable(value)
    value.command("send_test")
    pump(lambda: bool(reports))
    assert reports[-1]["phase"] == "test" and reports[-1]["result"] == "accepted"
    assert reports[-1]["event_ids"][0].startswith("test-")
    assert "fake-owner" not in str(reports) and "fake-context" not in str(reports)


def test_end_controls_have_reserved_bounded_ingress_even_with_full_mailbox(service):
    value, _, reports = service
    enable(value)
    pump(lambda: not value._inbox.scheduled)
    value._wake.disconnect(value.worker.drain)
    value.publish([AcceptedEvent(alarm(str(index)), "start") for index in range(100)])
    value.publish([AcceptedEvent(alarm("overflow"), "start")])
    value.publish([AcceptedEvent(replace(alarm(), ended_at=time.time(), end_reason="恢复正常"), "end")])
    with value._inbox.lock:
        assert len(value._inbox.events) == 100
        assert {item[0].id for item in value._inbox.events.values()} == {str(i) for i in range(100)}
        assert len(value._inbox.ends) == 1
        assert next(iter(value._inbox.ends.values()))[0].id == "a"
    assert reports[-1]["event_ids"] == ("overflow",) and reports[-1]["phase"] == "start"
    assert reports[-1]["result"] == "queue_full"


def test_update_flood_never_enters_mailbox_or_emits_delivery_records(service):
    value, facts, reports = service
    enable(value)
    pump(lambda: not value._inbox.scheduled)
    value._wake.disconnect(value.worker.drain)
    value.configure(True, 60)
    assert not value.reminders_enabled
    update = AcceptedEvent(alarm(), "update")
    value.publish([update] * 10_000)
    with value._inbox.lock:
        assert not value._inbox.events and not value._inbox.ends
        assert not value._inbox.scheduled
    assert not reports and not facts.calls
    value.publish([AcceptedEvent(alarm(), "start")])
    with value._inbox.lock:
        created = next(iter(value._inbox.events.values()))[4]
    value.publish([AcceptedEvent(alarm(), "start")] * 100)
    with value._inbox.lock:
        assert len(value._inbox.events) == 1
        assert next(iter(value._inbox.events.values()))[4] == created


def test_control_overflow_is_visible_and_safely_cancels_inflight(service):
    value, facts, reports = service
    facts.mode = "pending"
    enable(value)
    value.publish([AcceptedEvent(alarm("active"), "start")])
    pump(lambda: bool(facts.calls))
    value._wake.disconnect(value.worker.drain)
    value.publish([AcceptedEvent(replace(alarm(str(index)), ended_at=time.time(),
                                end_reason="恢复正常"), "end") for index in range(257)])
    with value._inbox.lock:
        assert len(value._inbox.ends) == 256 and value._inbox.overflow
        assert {item[0].id for item in value._inbox.ends.values()} == {str(i) for i in range(256)}
    assert reports[-1]["event_ids"] == ("256",) and reports[-1]["phase"] == "recovery"
    assert reports[-1]["result"] == "queue_full"
    value._wake.connect(value.worker.drain)
    value._wake.emit()
    pump(lambda: any(r["result"] == "cancelled" for r in reports))
    assert reports[-1]["event_ids"] == ("active",) and reports[-1]["phase"] == "start"
    assert not any(r["result"] == "accepted" for r in reports)
    assert value.snapshot["pending_count"] == 0
    assert not value.worker.controller.busy
