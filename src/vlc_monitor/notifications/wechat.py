"""Opt-in iLink adapter. No SDK cache or automatic login.

Wire fields are based on CoreSpeed/wechatbot 6914de4e4cae966b1a3f4c6531711c9dffd84fee.
See docs/wechat-adapter-notes.md for the audit, license, and unverified gates.
"""
from __future__ import annotations

import base64
import hashlib
import json
import math
import secrets
import time
from email.utils import parsedate_to_datetime
from urllib.parse import urlencode, urlsplit
import uuid

from .credentials import CredentialError

BASE_URL = "https://ilinkai.weixin.qq.com"
KEY = "primary-binding-v1"
TEST_TEXT = "VLC 监播工具：本人绑定测试通知（不是真实告警）。"
SENDABLE_STATES = {"context_unverified", "trial_sendable"}


def retry_after_seconds(value, now=None):
    """Return only finite, nonnegative numeric metadata, never a raw header."""
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        try:
            seconds = parsedate_to_datetime(value).timestamp() - (time.time() if now is None else now)
        except (TypeError, ValueError, OverflowError, AttributeError):
            return None
    return max(0.0, seconds) if math.isfinite(seconds) else None


def send_response_error(data):
    if not isinstance(data, dict):
        return "invalid_response"
    codes = [data[key] for key in ("ret", "errcode") if key in data]
    if any(type(code) is int and code in (-14, -2) for code in codes):
        return "session_invalid"
    if not codes or any(type(code) is not int or code != 0 for code in codes):
        return "api_rejected"
    return None


def validated_origin(value):
    parsed = urlsplit(value)
    if (parsed.scheme != "https" or parsed.hostname != "ilinkai.weixin.qq.com"
            or parsed.port not in (None, 443) or parsed.username or parsed.password
            or parsed.path not in ("", "/") or parsed.query or parsed.fragment):
        raise ValueError("Unapproved service origin")
    return BASE_URL


class QtJsonTransport:
    """Created/used only in the settings worker's Qt thread; bounded and cancellable."""
    def __init__(self, parent=None):
        from PySide6.QtNetwork import QNetworkAccessManager
        self.manager = QNetworkAccessManager(parent)
        self.pending = set()

    def request(self, method, origin, endpoint, payload, token, callback, timeout=15):
        from PySide6.QtCore import QByteArray, QTimer, QUrl
        from PySide6.QtNetwork import QNetworkReply, QNetworkRequest
        validated_origin(origin)
        if urlsplit(endpoint).path not in {"/ilink/bot/get_bot_qrcode", "/ilink/bot/get_qrcode_status", "/ilink/bot/getupdates", "/ilink/bot/sendmessage"} or "#" in endpoint:
            raise ValueError("Unapproved endpoint")
        request = QNetworkRequest(QUrl(origin + endpoint))
        request.setAttribute(QNetworkRequest.Attribute.RedirectPolicyAttribute, QNetworkRequest.RedirectPolicy.ManualRedirectPolicy)
        request.setTransferTimeout(timeout * 1000)
        request.setRawHeader(b"Content-Type", b"application/json")
        request.setRawHeader(b"iLink-App-Id", b"bot")
        request.setRawHeader(b"iLink-App-ClientVersion", b"256")
        if token:
            if any(ord(char) < 33 or ord(char) > 126 for char in token):
                raise ValueError("Invalid token encoding")
            request.setRawHeader(b"AuthorizationType", b"ilink_bot_token")
            request.setRawHeader(b"Authorization", b"Bearer " + token.encode("ascii"))
            request.setRawHeader(b"X-WECHAT-UIN", base64.b64encode(str(secrets.randbits(32)).encode()))
        if method == "GET":
            reply = self.manager.get(request)
        else:
            reply = self.manager.post(request, QByteArray(json.dumps(payload).encode()))
        self.pending.add(reply)
        connect_timer = QTimer(reply)
        connect_timer.setSingleShot(True)
        connect_timer.timeout.connect(reply.abort)
        reply.requestSent.connect(connect_timer.stop)
        connect_timer.start(5000)
        timer = QTimer(reply)
        timer.setSingleShot(True)
        timer.timeout.connect(reply.abort)
        timer.start(timeout * 1000)
        chunks = bytearray()
        too_large = [False]

        def drain():
            chunks.extend(bytes(reply.readAll()))
            if len(chunks) > 256_000:
                too_large[0] = True
                reply.abort()

        def done():
            timer.stop()
            connect_timer.stop()
            self.pending.discard(reply)
            drain()
            status = reply.attribute(QNetworkRequest.Attribute.HttpStatusCodeAttribute)
            error = None
            result = None
            if too_large[0]:
                error = "invalid_response"
            elif status is not None and 300 <= int(status) < 400:
                error = "redirect_rejected"
            elif status is not None and int(status) >= 400:
                if int(status) == 429 or 500 <= int(status) < 600:
                    error = "transient"
                    delay = retry_after_seconds(bytes(reply.rawHeader(b"Retry-After")).decode("ascii", errors="replace"))
                    result = {"retry_after": delay} if delay is not None else None
                else:
                    error = "session_invalid" if int(status) in (401, 403) else "request_failed"
            elif reply.error() != QNetworkReply.NetworkError.NoError:
                error = "uncertain" if method == "POST" and endpoint.endswith("sendmessage") else "network_failed"
            elif status != 200:
                error = "request_failed"
            else:
                try:
                    result = json.loads(chunks)
                    if not isinstance(result, dict):
                        raise ValueError()
                    if endpoint.endswith("sendmessage"):
                        error = send_response_error(result)
                    elif any(result.get(k, 0) != 0 for k in ("ret", "errcode")):
                        error = "session_invalid" if result.get("ret") in (-14, -2) or result.get("errcode") in (-14, -2) else "api_rejected"
                except (ValueError, TypeError):
                    error = "invalid_response"
            reply.deleteLater()
            callback(result if error in (None, "transient") else None, error)

        reply.readyRead.connect(drain)
        reply.finished.connect(done)

    def cancel(self):
        for reply in tuple(self.pending):
            reply.abort()


class WechatBindingController:
    """Single-thread owner; only explicit commands can initiate network operations."""
    def __init__(self, store, transport, changed=None, clock=time.monotonic):
        self.store = store
        self.transport = transport
        self.changed = changed or (lambda snapshot: None)
        self.clock = clock
        self.enabled = False
        self.generation = 0
        self.send_epoch = 0
        self.state = "disabled"
        self.result = ""
        self.binding = None
        self.login = None
        self.candidate = None
        self.challenge = ""
        self.qr_content = ""
        self.qr_id = ""
        self.deadline = 0
        self.cursor = ""
        self.busy = False
        self.test_accepted = False
        self.phone_confirmed = False
        self.deletion_failed = False
        self._alarm_inflight = None

    def snapshot(self):
        return {
            "state": self.state, "result": self.result, "enabled": self.enabled,
            "busy": self.busy, "challenge": self.challenge, "qr_content": self.qr_content,
            "candidate": hashlib.sha256(self.candidate["recipient"].encode()).hexdigest()[:8] if self.candidate else "",
            "test_accepted": self.test_accepted, "phone_confirmed": self.phone_confirmed,
            "deletion_failed": self.deletion_failed,
            "generation": self.generation, "send_epoch": self.send_epoch,
            "real_alarms_available": bool(self.enabled and self.binding and self.state in SENDABLE_STATES),
        }

    def _emit(self):
        self.changed(self.snapshot())

    def _clear_flow(self):
        self.login = self.candidate = None
        self.challenge = self.qr_content = self.qr_id = self.cursor = ""
        self.deadline = 0

    def disable(self):
        self.enabled = False
        self.generation += 1
        self.send_epoch += 1
        self.busy = False
        self._alarm_inflight = None
        self._clear_flow()
        self.binding = None
        self.test_accepted = self.phone_confirmed = False
        self.state = "disabled"
        self.transport.cancel()
        self._emit()

    def enable(self):
        if self.enabled:
            return
        if self.deletion_failed:
            self.result = "credential_delete_failed"
            self._emit()
            return
        self.generation += 1
        self.send_epoch += 1
        self.result = ""
        try:
            raw = self.store.load(KEY)
            binding = json.loads(raw) if raw else None
            if binding:
                required = ("token", "account", "owner", "recipient", "context", "reference", "origin")
                if not all(isinstance(binding.get(k), str) and binding[k] for k in required):
                    raise ValueError()
                validated_origin(binding["origin"])
                if binding["owner"] != binding["recipient"]:
                    raise ValueError()
            self.binding = binding
        except (CredentialError, ValueError, TypeError, KeyError):
            self.state = "credential_unavailable"
            self.result = "credential_read_failed"
            self._emit()
            return
        self.enabled = True
        self.state = "context_unverified" if self.binding else "unbound"
        self._emit()

    def _request(self, method, endpoint, body, handler, *, login=False, timeout=15):
        if not self.enabled or self.busy:
            return
        stamp = self.generation, self.send_epoch
        auth = self.login if login else self.binding
        origin = auth["origin"] if auth else BASE_URL
        token = auth["token"] if auth else ""
        self.busy = True
        self._emit()

        def complete(payload, error):
            if stamp != (self.generation, self.send_epoch) or not self.enabled:
                return
            self.busy = False
            if not error and endpoint.endswith("sendmessage"):
                error = send_response_error(payload)
            if error:
                self.result = error
                if error == "session_invalid":
                    self.state = "session_invalid"
                    self._clear_flow()
                elif self.state in ("waiting_scan", "waiting_challenge"):
                    self.state = "failed"
                    self._clear_flow()
                self._emit()
                return
            try:
                handler(payload)
            except (KeyError, TypeError, ValueError, UnicodeError):
                self.result = "invalid_response"
                self.state = "failed"
                self._clear_flow()
            self._emit()

        try:
            self.transport.request(method, origin, endpoint, body, token, complete, timeout=timeout)
        except (ValueError, RuntimeError, UnicodeError):
            complete(None, "request_failed")

    def start_binding(self):
        if not self.enabled or self.busy or self.binding:
            return
        self.generation += 1
        self._clear_flow()
        self.test_accepted = self.phone_confirmed = False
        self.state = "waiting_scan"
        self.result = ""
        self.deadline = self.clock() + 180

        def received(data):
            if self.clock() >= self.deadline:
                self._expire()
                return
            if not all(isinstance(data.get(k), str) and 0 < len(data[k].encode()) <= 1024 for k in ("qrcode", "qrcode_img_content")):
                raise ValueError()
            self.qr_id = data["qrcode"]
            self.qr_content = data["qrcode_img_content"]

        self._request("POST", "/ilink/bot/get_bot_qrcode?bot_type=3", {"local_token_list": []}, received)

    def _expire(self):
        self.generation += 1
        self.busy = False
        self._clear_flow()
        self.state = "expired"
        self.result = "binding_expired"
        self.transport.cancel()

    def tick(self):
        if not self.enabled:
            return
        if self.state in ("waiting_scan", "waiting_challenge", "waiting_confirmation") and self.clock() >= self.deadline:
            self._expire()
            self._emit()
            return
        if self.busy:
            return
        if self.state == "waiting_scan" and self.qr_id:
            self._request("GET", "/ilink/bot/get_qrcode_status?" + urlencode({"qrcode": self.qr_id}), None, self._login_received)
        elif self.state == "waiting_challenge":
            self._request("POST", "/ilink/bot/getupdates", {"get_updates_buf": self.cursor, "base_info": {"channel_version": "0.1.0", "bot_agent": "VLCMonitor/0.1.0"}}, self._updates_received, login=True, timeout=45)

    def _login_received(self, data):
        if self.clock() >= self.deadline:
            self._expire()
            return
        status = data.get("status")
        if status == "confirmed":
            values = {"token": data.get("bot_token"), "account": data.get("ilink_bot_id"), "owner": data.get("ilink_user_id"), "origin": validated_origin(data.get("baseurl") or BASE_URL)}
            if not all(isinstance(v, str) and 0 < len(v) <= 8192 for v in values.values()):
                raise ValueError()
            self.login = values
            self.qr_content = self.qr_id = ""
            self.challenge = "VLC-" + secrets.token_hex(12)
            self.deadline = self.clock() + 180
            self.state = "waiting_challenge"
        elif status in ("need_verifycode", "verify_code_blocked", "scaned_but_redirect", "binded_redirect"):
            # Pair-code and alternate IDC origin are unverified, never guessed/retried.
            self._clear_flow()
            self.state = "blocked"
            self.result = "unsupported_login_branch"
        elif status == "expired":
            self._expire()
        elif status not in ("wait", "scaned"):
            raise ValueError()

    def _updates_received(self, data):
        if self.clock() >= self.deadline:
            self._expire()
            return
        cursor = data.get("get_updates_buf", "")
        if not isinstance(cursor, str) or len(cursor) > 8192:
            raise ValueError()
        self.cursor = cursor
        messages = data.get("msgs", [])
        if not isinstance(messages, list):
            raise ValueError()
        for message in messages[:100]:
            if not isinstance(message, dict) or message.get("message_type") != 1:
                continue
            if message.get("from_user_id") != self.login["owner"] or message.get("to_user_id") != self.login["account"]:
                continue
            items = message.get("item_list", [])
            if not isinstance(items, list) or len(items) != 1 or not isinstance(items[0], dict):
                continue
            text_item = items[0].get("text_item", {})
            if items[0].get("type") != 1 or not isinstance(text_item, dict):
                continue
            text = text_item.get("text")
            if not isinstance(text, str) or not secrets.compare_digest(text.encode(), self.challenge.encode()):
                continue
            context = message.get("context_token")
            if not isinstance(context, str) or not 0 < len(context) <= 8192:
                continue
            self.candidate = {**self.login, "recipient": message["from_user_id"], "context": context, "reference": uuid.uuid4().hex}
            self.challenge = ""
            self.state = "waiting_confirmation"
            break

    def confirm_candidate(self):
        if not self.enabled or self.state != "waiting_confirmation" or not self.candidate:
            return
        if self.clock() >= self.deadline:
            self._expire()
            self._emit()
            return
        try:
            raw = json.dumps(self.candidate, separators=(",", ":")).encode()
            self.store.save(KEY, raw)
            if self.store.load(KEY) != raw:
                raise CredentialError("Read-back mismatch")
        except CredentialError:
            self.disable()
            try:
                self.store.delete(KEY)
                if self.store.load(KEY) is not None:
                    raise CredentialError("Cleanup failed")
            except CredentialError:
                self.deletion_failed = True
            self.state = "credential_unavailable"
            self.result = "credential_write_failed"
            self._emit()
            return
        self.binding = self.candidate
        self._clear_flow()
        self.state = "context_unverified"
        self.result = "binding_saved"
        self._emit()

    def send_test(self):
        if not self.enabled or self.busy or not self.binding or self.state not in SENDABLE_STATES:
            return
        self.test_accepted = self.phone_confirmed = False
        self.result = "test_pending"
        message = {"from_user_id": "", "to_user_id": self.binding["recipient"], "client_id": uuid.uuid4().hex, "message_type": 2, "message_state": 2, "context_token": self.binding["context"], "item_list": [{"type": 1, "text_item": {"text": TEST_TEXT}}]}

        def accepted(data):
            if send_response_error(data):
                raise ValueError()
            self.state = "trial_sendable"
            self.result = "test_api_accepted"
            self.test_accepted = True

        self._request("POST", "/ilink/bot/sendmessage", {"msg": message, "base_info": {"channel_version": "0.1.0", "bot_agent": "VLCMonitor/0.1.0"}}, accepted)

    def send_alarm(self, text, client_id, callback):
        """One attempt only; the service owns ordering, retry budgets and TTL."""
        if not self.snapshot()["real_alarms_available"]:
            callback("session_invalid" if self.state == "session_invalid" else "failed", None)
            return False
        if self.busy:
            callback("transient", None)
            return False
        if not isinstance(text, str) or not text or not isinstance(client_id, str) or not client_id:
            callback("failed", None)
            return False
        stamp = self.generation, self.send_epoch
        identity = object()
        self._alarm_inflight = identity
        message = {"from_user_id": "", "to_user_id": self.binding["recipient"], "client_id": client_id,
                   "message_type": 2, "message_state": 2, "context_token": self.binding["context"],
                   "item_list": [{"type": 1, "text_item": {"text": text}}]}
        self.busy = True
        self._emit()

        def complete(payload, error):
            if stamp != (self.generation, self.send_epoch) or not self.enabled or self._alarm_inflight is not identity:
                return
            self._alarm_inflight = None
            self.busy = False
            error = error or send_response_error(payload)
            result = {None: "accepted", "uncertain": "uncertain", "transient": "transient",
                      "session_invalid": "session_invalid"}.get(error, "failed")
            retry_after = retry_after_seconds(payload.get("retry_after")) if result == "transient" and isinstance(payload, dict) else None
            if result == "accepted":
                self.state = "trial_sendable"
            elif result == "session_invalid":
                self.state = "session_invalid"
                self._clear_flow()
            self.result = "alarm_api_accepted" if result == "accepted" else "alarm_" + result
            callback(result, retry_after)
            self._emit()

        try:
            self.transport.request("POST", self.binding["origin"], "/ilink/bot/sendmessage",
                                   {"msg": message, "base_info": {"channel_version": "0.1.0", "bot_agent": "VLCMonitor/0.1.0"}},
                                   self.binding["token"], complete)
        except (ValueError, RuntimeError, UnicodeError):
            complete(None, "request_failed")
        return True

    def cancel_alarm(self):
        """Cancel only an alarm; binding and manual test requests are untouched."""
        if self._alarm_inflight is None:
            return
        self._alarm_inflight = None
        self.busy = False
        self.transport.cancel()
        self._emit()

    def confirm_phone_receipt(self):
        if self.enabled and self.test_accepted:
            self.phone_confirmed = True
            self.result = "phone_receipt_confirmed_by_user"
            self._emit()

    def unbind(self):
        self.disable()
        try:
            self.store.delete(KEY)
            if self.store.load(KEY) is not None:
                raise CredentialError("Deletion read-back mismatch")
        except CredentialError:
            self.deletion_failed = True
            self.result = "credential_delete_failed"
        else:
            self.deletion_failed = False
            self.result = "unbound"
        self._emit()
