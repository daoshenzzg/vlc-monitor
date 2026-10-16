"""Bounded asynchronous history persistence; SQLite has exactly one owner."""
from collections import deque
import sqlite3
import threading
import time

from PySide6.QtCore import QObject, QThread, Signal, Slot

from .event_store import EventStore


class _Mailbox:
    def __init__(self, capacity, close_timeout):
        self.condition = threading.Condition()
        self.records = deque()
        self.capacity = capacity
        self.pending = 0  # Includes the worker's in-flight batch.
        self.closing = False
        self.deadline = None
        self.close_timeout = close_timeout
        self.paused = False
        self.discard = False
        self.unavailable = False
        self.memory_only = False
        self.reported = False
        self.retry_initialization = False


class _StorageThread(QThread):
    loaded = Signal(object)
    problem = Signal(str)
    close_failed = Signal(str)

    def __init__(self, path, mailbox, batch_size, factory, parent):
        super().__init__(parent)
        self.path, self.mailbox = path, mailbox
        self.batch_size, self.factory = batch_size, factory

    @staticmethod
    def _attempt(operation):
        for attempt in range(2):
            try:
                return operation()
            except sqlite3.OperationalError as exc:
                if attempt or not any(word in str(exc).lower() for word in ("locked", "busy")):
                    raise

    def _problem(self):
        box = self.mailbox
        with box.condition:
            box.memory_only = True
            notify = not box.reported
            box.reported = True
        if notify:
            self.problem.emit("部分历史未保存，仅内存有效；本地与微信告警不受影响。")

    def run(self):
        box = self.mailbox
        store = None
        try:
            try:
                store = self._attempt(lambda: self.factory(self.path))
            except Exception:
                with box.condition:
                    box.unavailable = True
                self._problem()
            while True:
                failed_message = None
                with box.condition:
                    while not box.discard and (box.paused or (not box.records and not box.closing)):
                        box.condition.wait()
                    if box.discard:
                        box.records.clear()
                        box.pending = 0
                        break
                    if box.closing and not box.records:
                        break
                    if box.closing and time.monotonic() >= box.deadline:
                        box.paused = True
                        failed_message = "历史保存超时：请重试或明确放弃未保存数据。"
                        batch = []
                    else:
                        batch = [box.records.popleft()]
                        # A read is a barrier: results never overtake preceding writes.
                        if batch[0][0] != "recent":
                            while box.records and len(batch) < self.batch_size and box.records[0][0] != "recent":
                                batch.append(box.records.popleft())
                    retry_initialization = box.retry_initialization
                    box.retry_initialization = False
                if failed_message:
                    self.close_failed.emit(failed_message)
                    continue
                failed = False
                try:
                    if store is None and retry_initialization:
                        store = self._attempt(lambda: self.factory(self.path))
                    if store is None:
                        failed = True
                    elif batch[0][0] == "recent":
                        self.loaded.emit(self._attempt(lambda: store.recent(batch[0][1][0])))
                    else:
                        self._attempt(lambda: store.write_batch(batch))
                except Exception:
                    failed = True
                if failed:
                    self._problem()
                with box.condition:
                    if failed and box.closing and not box.discard:
                        box.records.extendleft(reversed(batch))
                        box.paused = True
                        failed_message = "历史保存失败：请重试或明确放弃未保存数据。"
                    else:
                        box.pending -= len(batch)
                if failed_message:
                    self.close_failed.emit(failed_message)
        finally:
            if store is not None:
                try:
                    store.close()
                except Exception:
                    self._problem()


class EventService(QObject):
    loaded = Signal(object)
    problem = Signal(str)
    closed = Signal()
    close_failed = Signal(str)

    def __init__(self, path, parent=None, *, capacity=1024, batch_size=64,
                 close_timeout=2.0, store_factory=EventStore):
        super().__init__(parent)
        if not 1 <= capacity <= 1024 or not 1 <= batch_size <= 64 or close_timeout <= 0:
            raise ValueError("Invalid storage budget")
        self._box = _Mailbox(capacity, close_timeout)
        self.is_closed = False
        self.thread = _StorageThread(path, self._box, batch_size, store_factory, self)
        self.thread.loaded.connect(self.loaded)
        self.thread.problem.connect(self.problem)
        self.thread.close_failed.connect(self.close_failed)
        self.thread.finished.connect(self._closed)
        self.thread.start()

    @property
    def closing(self):
        with self._box.condition:
            return self._box.closing

    @property
    def pending_count(self):
        with self._box.condition:
            return self._box.pending

    @property
    def memory_only(self):
        with self._box.condition:
            return self._box.memory_only

    def _submit(self, records):
        box = self._box
        accepted, notify = True, False
        with box.condition:
            if box.closing or self.is_closed:
                return False
            # Never materialize an unbounded caller list or hide it inside a command.
            for index, record in enumerate(records):
                if index >= box.capacity or box.pending >= box.capacity or box.unavailable:
                    accepted = False
                    box.memory_only = True
                    notify = not box.reported
                    box.reported = True
                    break
                box.records.append(record)
                box.pending += 1
            box.condition.notify()  # Condition wakeups coalesce; no per-record Qt event.
        if notify:
            self.problem.emit("部分历史未保存，仅内存有效；存储不可用或待保存队列已满。")
        return accepted

    def upsert(self, events):
        return self._submit(EventStore.event_record(event) for event in events)

    def recent(self, limit=200):
        return self._submit((("recent", (max(0, min(1000, int(limit))),)),))

    def acknowledge(self, event_ids, acknowledged_at):
        return self._submit(("acknowledge", (str(event_id), float(acknowledged_at))) for event_id in event_ids)

    def record_delivery(self, event_ids, channel, phase, result, sequence=0, attempts=1, created_at=None):
        EventStore.validate_delivery(channel, phase, result)
        stamp = time.time() if created_at is None else float(created_at)
        return self._submit(("delivery", (str(event_id), channel, phase, int(sequence), int(attempts), result, stamp)) for event_id in event_ids)

    def cleanup(self):
        return self._submit((("cleanup", ()),))

    def begin_close(self):
        """Seal only AFTER all producers' final GUI callbacks have been received."""
        box = self._box
        with box.condition:
            if box.closing or self.is_closed:
                return
            box.closing = True
            box.deadline = time.monotonic() + box.close_timeout
            box.condition.notify()

    def retry_close(self):
        box = self._box
        with box.condition:
            if not box.closing or self.is_closed:
                return
            box.deadline = time.monotonic() + box.close_timeout
            box.paused = False
            box.retry_initialization = True
            box.condition.notify()

    def discard_close(self):
        """Explicit user consent to drop pending records, never terminate a worker."""
        box = self._box
        notify = False
        with box.condition:
            if self.is_closed:
                return
            if box.pending:
                box.memory_only = True
                notify = not box.reported
                box.reported = True
            box.closing = True
            box.discard = True
            box.condition.notify()
        if notify:
            self.problem.emit("已放弃待保存历史，仅内存有效。")

    @Slot()
    def _closed(self):
        self.is_closed = True
        self.closed.emit()
