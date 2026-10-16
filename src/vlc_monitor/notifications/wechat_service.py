"""Application-lifetime WeChat actor; no widgets or SQLite in this worker."""
from collections import OrderedDict
from dataclasses import replace
from pathlib import Path
import threading
import time
import uuid

from PySide6.QtCore import QObject, QThread, QTimer, Signal, Slot, Qt

from .credentials import CredentialError, GuardedCredentialStore, system_credential_store
from .wechat import QtJsonTransport, WechatBindingController
from .wechat_queue import WechatDeliveryQueue


class _Inbox:
    """Bound the Qt handoff too, including while an OS credential prompt is open."""
    def __init__(self):
        self.lock = threading.Lock()
        self.context = ("", False, (), (), 0)
        self.events = OrderedDict()
        self.ends = OrderedDict()
        self.overflow = False
        self.scheduled = False

    def take(self):
        with self.lock:
            context = self.context
            events = tuple(self.events.values()) + tuple(self.ends.values())
            if self.overflow:
                session, _, regions, editing, run_id = context
                context = (session, False, regions, editing, run_id)
            self.events.clear()
            self.ends.clear()
            self.overflow = False
            self.scheduled = False
        return context, events


class _WechatWorker(QObject):
    updated = Signal(object)
    delivery = Signal(object)
    stopped = Signal()

    def __init__(self, inbox, marker, reminders, repeat_seconds, factory=None):
        super().__init__()
        self.inbox, self.marker = inbox, marker
        self.reminders, self.repeat_seconds = False, 60
        self.factory = factory
        self.serial = 0
        self.shutting_down = False
        self.last_delivery = ""
        self.test_request = None
        self.binding_due = 0

    @Slot()
    def initialize(self):
        self.queue = WechatDeliveryQueue(report=self._report)
        self.queue.configure(self.reminders, self.repeat_seconds)
        if self.factory:
            store, transport = self.factory(self)
        else:
            store = GuardedCredentialStore(system_credential_store, self.marker)
            transport = QtJsonTransport(self)
        self.controller = WechatBindingController(store, transport, self._changed)
        self.timer = QTimer(self)
        self.timer.setInterval(200)
        self.timer.timeout.connect(self.tick)
        self.timer.start()
        self._changed(self.controller.snapshot())

    def _changed(self, snapshot):
        if self.test_request and not snapshot["busy"]:
            event_id, generation, epoch = self.test_request
            stale = (generation, epoch) != (snapshot["generation"], snapshot["send_epoch"])
            result = "cancelled" if stale else {"test_api_accepted": "accepted", "uncertain": "uncertain"}.get(snapshot["result"], "failed")
            self.test_request = None
            self.delivery.emit({"event_ids": (event_id,), "generation": generation, "send_epoch": epoch,
                "phase": "test", "sequence": 0, "attempts": 1, "result": result})
        self.queue.set_channel(snapshot["generation"], snapshot["send_epoch"],
                               snapshot["real_alarms_available"] and not self.shutting_down)
        value = dict(snapshot, serial=self.serial, pending_count=self.queue.pending_count,
                     delivery_result=self.last_delivery)
        self.updated.emit(value)

    def _report(self, record):
        self.last_delivery = record["result"]
        self.delivery.emit(record)

    @Slot(object)
    def command(self, request):
        action, self.serial = request
        if action == "shutdown":
            self.shutting_down = True
            self.timer.stop()
            self.controller.disable()
            self.stopped.emit()
            return
        if self.shutting_down:
            return
        allowed = {"enable", "disable", "start_binding", "confirm_candidate", "send_test", "confirm_phone_receipt", "unbind"}
        if action not in allowed:
            return
        self.last_delivery = ""
        if action == "send_test" and self.controller.snapshot()["real_alarms_available"] and not self.controller.busy:
            self.test_request = ("test-" + uuid.uuid4().hex, self.controller.generation, self.controller.send_epoch)
        try:
            getattr(self.controller, action)()
        except (CredentialError, OSError, RuntimeError, ValueError):
            self.controller.disable()
            self.controller.result = "request_failed"
        self._changed(self.controller.snapshot())

    @Slot(object)
    def configure(self, settings):
        old = self.queue.inflight
        self.queue.configure(*settings)
        if old is not None and self.queue.inflight is not old:
            self.controller.cancel_alarm()
        self._changed(self.controller.snapshot())

    @Slot()
    def drain(self):
        context, events = self.inbox.take()
        if self.shutting_down:
            return
        old = self.queue.inflight
        session, running, regions, editing, run_id = context
        self.queue.set_context(session, running, regions, editing, run_id=run_id)
        stamp = (self.controller.generation, self.controller.send_epoch)
        for event, phase, generation, epoch, created in events:
            if (generation, epoch) == stamp:
                self.queue.ingest(event, phase, created_at=created)
        if old is not None and self.queue.inflight is not old:
            self.controller.cancel_alarm()
        self._changed(self.controller.snapshot())

    @Slot()
    def tick(self):
        if self.shutting_down:
            return
        now = time.monotonic()
        if now >= self.binding_due:
            self.binding_due = now + 2
            self.controller.tick()
        if self.controller.busy or not self.controller.snapshot()["real_alarms_available"]:
            return
        task = self.queue.tick()
        if task is None:
            return

        def complete(result, retry_after):
            self.queue.complete(task, result, retry_after=retry_after or 0)
            self._changed(self.controller.snapshot())

        if not self.controller.send_alarm(task.text, task.client_id, complete):
            self.queue.complete(task, "failed")
        self._changed(self.controller.snapshot())


class WechatNotificationService(QObject):
    updated = Signal(object)
    delivery = Signal(object)
    settingsChanged = Signal(object)
    stopped = Signal()
    _command = Signal(object)
    _configure = Signal(object)
    _wake = Signal()

    def __init__(self, data_dir, *, enabled=False, reminders_enabled=False,
                 repeat_seconds=60, parent=None, factory=None):
        super().__init__(parent)
        self.reminders_enabled = False
        self.repeat_seconds = 60
        self.snapshot = {"state": "disabled", "result": "", "enabled": False,
                         "busy": False, "challenge": "", "qr_content": "", "candidate": "",
                         "test_accepted": False, "phone_confirmed": False,
                         "deletion_failed": False, "generation": 0, "send_epoch": 0,
                         "real_alarms_available": False, "pending_count": 0, "serial": 0}
        self._serial = 0
        self._closing = False
        self._inbox = _Inbox()
        self.thread = QThread(self)
        self.worker = _WechatWorker(self._inbox, Path(data_dir) / "wechat-cleanup-required",
                                    False, 60, factory)
        self.worker.moveToThread(self.thread)
        self.thread.started.connect(self.worker.initialize)
        self._command.connect(self.worker.command)
        self._configure.connect(self.worker.configure)
        self._wake.connect(self.worker.drain)
        self.worker.updated.connect(self._update)
        self.worker.delivery.connect(self._delivery)
        self.worker.stopped.connect(self.thread.quit, Qt.ConnectionType.DirectConnection)
        self.worker.stopped.connect(self.worker.deleteLater)
        self.thread.finished.connect(self.stopped)
        self.thread.start()
        if enabled:
            self.command("enable")

    @Slot(object)
    def _update(self, snapshot):
        if snapshot["serial"] < self._serial:
            return
        self.snapshot = snapshot
        self.updated.emit(snapshot)

    @Slot(object)
    def _delivery(self, result):
        # Archive sanitized historical outcomes, never use them to change a new binding.
        self.delivery.emit(result)

    def command(self, action):
        if self._closing:
            return
        self._serial += 1
        # Stop new ingress synchronously; worker callbacks cannot reopen this gate.
        if action in ("enable", "disable", "unbind"):
            self.snapshot = dict(self.snapshot, real_alarms_available=False)
        if action in ("disable", "unbind"):
            self.snapshot = dict(self.snapshot, enabled=False)
        if action in ("enable", "disable", "unbind"):
            self.settingsChanged.emit({"wechat_enabled": action == "enable"})
        self._command.emit((action, self._serial))

    def configure(self, reminders_enabled=False, repeat_seconds=60):
        # Keep the call shape for older callers, but never reactivate repeats.
        self.reminders_enabled = False
        self.repeat_seconds = 60
        self._configure.emit((self.reminders_enabled, self.repeat_seconds))
        self.settingsChanged.emit({"wechat_reminders_enabled": self.reminders_enabled,
                                   "wechat_repeat_seconds": self.repeat_seconds})

    def set_context(self, session_id, running, regions, editing_ids=(), run_id=0):
        if self._closing:
            return
        with self._inbox.lock:
            self._inbox.context = (session_id, bool(running), tuple(replace(r) for r in regions),
                                   tuple(editing_ids), run_id)
            wake = not self._inbox.scheduled
            self._inbox.scheduled = True
        if wake:
            self._wake.emit()

    def publish(self, accepted):
        if self._closing or not self.snapshot["real_alarms_available"]:
            return
        wake = False
        for item in accepted:
            if item.phase not in ("start", "end") or item.event.anomaly_type not in ("black", "freeze", "stream"):
                continue
            key = (self.snapshot["generation"], self.snapshot["send_epoch"],
                   item.event.session_id, item.event.run_id, item.event.id, item.phase)
            with self._inbox.lock:
                # Reserve end capacity for recovery and non-normal cancellation.
                target = self._inbox.ends if item.phase == "end" else self._inbox.events
                limit = 256 if item.phase == "end" else 100
                full = key not in target and len(target) >= limit
                if full and item.phase == "end":
                    self._inbox.overflow = True  # Fail closed: cancel all instead of retaining false active alarms.
                if not full:
                    original = target.get(key)
                    target[key] = (replace(item.event), item.phase,
                        self.snapshot["generation"], self.snapshot["send_epoch"], original[4] if original else time.monotonic())
                    wake = wake or not self._inbox.scheduled
                    self._inbox.scheduled = True
            if full:
                self.delivery.emit({"event_ids": (item.event.id,), "phase": "recovery" if item.phase == "end" else "start",
                    "generation": self.snapshot["generation"], "send_epoch": self.snapshot["send_epoch"],
                    "sequence": 0, "attempts": 0, "result": "queue_full"})
        if wake:
            self._wake.emit()

    def _cancel_mailbox(self):
        with self._inbox.lock:
            pending = tuple(self._inbox.events.values()) + tuple(self._inbox.ends.values())
            self._inbox.events.clear()
            self._inbox.ends.clear()
            self._inbox.overflow = False
        # A worker-taken entry is no longer in this snapshot and reports there.
        for event, phase, generation, epoch, _ in pending:
            self.delivery.emit({"event_ids": (event.id,),
                "phase": "recovery" if phase == "end" else "start",
                "generation": generation, "send_epoch": epoch,
                "sequence": 0, "attempts": 0, "result": "cancelled"})

    def shutdown(self):
        if self._closing or not self.thread.isRunning():
            return
        self._closing = True
        self._serial += 1
        self.snapshot = dict(self.snapshot, real_alarms_available=False)
        self._cancel_mailbox()
        self._command.emit(("shutdown", self._serial))

    def quiesce(self):
        """Stop sending now, but keep the actor reusable if player shutdown fails."""
        if self._closing:
            return
        self._serial += 1
        self.snapshot = dict(self.snapshot, real_alarms_available=False, enabled=False)
        self._cancel_mailbox()
        self._command.emit(("disable", self._serial))
