"""Temporary SQLite only; deterministic worker failure and shutdown coverage."""
import os
import sqlite3
import threading
import time
from dataclasses import replace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
import pytest
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication

from vlc_monitor.models import AlarmEvent
from vlc_monitor.storage.event_service import EventService
from vlc_monitor.storage.event_store import EventStore


def pump(predicate, timeout=4):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        QApplication.processEvents()
        if predicate():
            return
        time.sleep(.002)
    assert predicate()


@pytest.fixture
def services(tmp_path):
    app = QApplication.instance() or QApplication([])
    active = []

    def create(**kwargs):
        service = EventService(tmp_path / (str(len(active)) + ".sqlite3"), **kwargs)
        active.append(service)
        return service

    yield create
    for service in active:
        service.discard_close()
        pump(lambda: service.is_closed)


def event(key="event"):
    now = time.time()
    return AlarmEvent(key, "s", "r", "region", "black", 0, now, now, now, 4)


def test_single_owner_immutable_snapshots_batch_order_and_last_receipt(services):
    owners, sizes = [], []

    class TrackingStore(EventStore):
        def __init__(self, path):
            owners.append(threading.get_ident())
            super().__init__(path)

        def write_batch(self, records):
            records = tuple(records)
            owners.append(threading.get_ident())
            sizes.append(len(records))
            super().write_batch(records)

        def close(self):
            owners.append(threading.get_ident())
            super().close()

    service = services(store_factory=TrackingStore)
    rows = []
    service.loaded.connect(rows.append)
    original = event()
    immutable = event("immutable")
    service.upsert([original, immutable])
    immutable.region_name = "must not persist"
    service.upsert(event("batch-" + str(i)) for i in range(130))
    original.region_name = "mutated after handoff"
    service.acknowledge([original.id], 123)
    service.upsert([replace(original, ended_at=original.triggered_at + 1)])
    service.upsert([original])  # Cannot reopen an ended event.
    service.cleanup()
    service.record_delivery([original.id], "wechat", "recovery", "accepted", sequence=2, attempts=3, created_at=456)
    service.recent()
    service.begin_close()
    service.begin_close()
    assert not service.upsert([event("late")])
    pump(lambda: service.is_closed)
    archived = {row["id"]: row for row in rows[0]}
    assert archived["immutable"]["region_name"] == "region"
    assert archived["event"]["ended_at"] is not None
    assert archived["event"]["acknowledged_at"] == 123
    assert archived["event"]["delivery"] == {"channel": "wechat", "result": "accepted", "created_at": 456}
    assert set(owners) != {threading.get_ident()} and len(set(owners)) == 1
    assert max(sizes) == 64
    with sqlite3.connect(service.thread.path) as db:
        assert db.execute("SELECT sequence,attempts,created FROM notification_delivery").fetchone() == (2, 3, 456)


def test_record_capacity_counts_inflight_and_single_large_submission(services):
    entered, release = threading.Event(), threading.Event()

    class BlockedStore(EventStore):
        def write_batch(self, records):
            records = tuple(records)
            if records and records[0][0] == "upsert":
                entered.set()
                assert release.wait(3)
            super().write_batch(records)

    service = services(store_factory=BlockedStore, capacity=8, batch_size=4)
    problems = []
    service.problem.connect(problems.append)
    try:
        service.upsert([event("first")])
        pump(entered.is_set)
        assert not service.upsert(event(str(i)) for i in range(10000))
        assert service.pending_count == 8  # Seven queued plus the held transaction.
        for _ in range(10):
            assert not service.acknowledge(["first"], 12)
        assert len(problems) == 1 and service.memory_only
    finally:
        release.set()
    service.begin_close()
    pump(lambda: service.is_closed)
    with sqlite3.connect(service.thread.path) as db:
        assert db.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 8


def test_real_sqlite_lock_keeps_gui_responsive_and_retry_closes(services):
    service = services()
    rows, failures, beats = [], [], []
    service.loaded.connect(rows.append)
    service.close_failed.connect(failures.append)
    service.recent()
    pump(lambda: bool(rows))
    locker = sqlite3.connect(service.thread.path)
    locker.execute("BEGIN IMMEDIATE")
    timer = QTimer()
    timer.setInterval(5)
    timer.timeout.connect(lambda: beats.append(time.monotonic()))
    timer.start()
    try:
        started = time.monotonic()
        assert service.upsert(event(str(i)) for i in range(100))
        assert service.acknowledge(["0"], 123)
        assert service.cleanup()
        service.begin_close()
        submit_elapsed = time.monotonic() - started
        pump(lambda: bool(failures))
        assert submit_elapsed < .1
        assert len(beats) >= 10
        assert not service.is_closed and service.pending_count == 102
        locker.rollback()
        service.retry_close()
        pump(lambda: service.is_closed)
        print(f"storage lock: submit={submit_elapsed:.4f}s, GUI heartbeats={len(beats)}")
    finally:
        timer.stop()
        locker.close()
    with sqlite3.connect(service.thread.path) as db:
        assert db.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 100
        assert db.execute("SELECT acknowledged_at FROM alarm_attention WHERE event_id='0'").fetchone()[0] == 123


def test_initialization_failure_and_write_failure_are_visible_and_bounded(services):
    def unavailable(path):
        raise OSError("synthetic disk failure")

    broken = services(store_factory=unavailable)
    problems = []
    broken.problem.connect(problems.append)
    pump(lambda: bool(problems))
    assert broken.memory_only
    assert not broken.upsert([event()])
    broken.begin_close()
    pump(lambda: broken.is_closed)

    class DiskFull(EventStore):
        def write_batch(self, records):
            records = tuple(records)
            if records[0][0] == "upsert":
                raise sqlite3.OperationalError("database or disk is full")
            super().write_batch(records)

    release = threading.Event()

    def full_disk(path):
        assert release.wait(3)
        return DiskFull(path)

    service = services(store_factory=full_disk)
    errors, failures = [], []
    service.problem.connect(errors.append)
    service.close_failed.connect(failures.append)
    service.upsert([event()])
    service.begin_close()
    release.set()
    pump(lambda: bool(failures))
    assert len(errors) == 1 and service.pending_count == 1
    service.discard_close()
    pump(lambda: service.is_closed)
    assert service.pending_count == 0


def test_flush_budget_pauses_until_explicit_discard(services):
    release = threading.Event()

    def delayed(path):
        assert release.wait(3)
        return EventStore(path)

    service = services(close_timeout=.000001, store_factory=delayed)
    failures = []
    service.close_failed.connect(failures.append)
    service.upsert([event()])
    service.begin_close()
    release.set()
    pump(lambda: bool(failures))
    assert not service.is_closed
    service.discard_close()
    pump(lambda: service.is_closed)
