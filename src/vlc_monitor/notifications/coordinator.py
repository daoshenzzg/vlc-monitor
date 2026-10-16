"""UI-independent activity and reminder policy; use from one owner thread."""

from collections import OrderedDict
from dataclasses import dataclass, replace
import time
from typing import Callable, Iterable

from vlc_monitor.models import AlarmEvent, RegionConfig


@dataclass(frozen=True)
class AcceptedEvent:
    event: AlarmEvent
    phase: str  # start / update / end / test / interruption


@dataclass(frozen=True)
class ReminderBatch:
    events: tuple[AlarmEvent, ...]
    phase: str  # first / repeat; only first may request foreground


class AlarmCoordinator:
    def __init__(self, clock: Callable[[], float] = time.monotonic,
                 aggregate_seconds: float = 0.3, repeat_seconds: float = 5.0,
                 closed_limit: int = 2048):
        if aggregate_seconds < 0 or repeat_seconds <= 0 or closed_limit < 1:
            raise ValueError("Invalid reminder timing or closed-ID bound")
        self.clock = clock
        self.aggregate_seconds = aggregate_seconds
        self.repeat_seconds = repeat_seconds
        self.closed_limit = closed_limit
        self.active: dict[str, AlarmEvent] = {}
        self.acknowledged_at: dict[str, float] = {}
        self.closed: OrderedDict[tuple[str, int, str], None] = OrderedDict()
        self._pending: set[str] = set()
        self._tests: dict[str, AlarmEvent] = {}
        self._first_due: float | None = None
        self._repeat_due: dict[str, float] = {}
        self._session_id = ""
        self._run_id = 0
        self._running = False
        self._regions: dict[str, RegionConfig] = {}
        self._editing: set[str] = set()
        self._shutdown = False
        self._suspended = False

    @staticmethod
    def _key(event):
        return event.session_id, getattr(event, "run_id", 0), event.id

    def _close(self, event):
        key = self._key(event)
        self.closed[key] = None
        self.closed.move_to_end(key)
        while len(self.closed) > self.closed_limit:
            self.closed.popitem(last=False)

    def _eligible(self, event):
        if (self._shutdown or not self._running
                or event.session_id != self._session_id
                or getattr(event, "run_id", 0) != self._run_id):
            return False
        if event.anomaly_type == "stream":
            return True
        region = self._regions.get(event.region_id)
        return bool(region and region.enabled and region.id not in self._editing
                    and region.revision == event.revision
                    and event.anomaly_type in ("black", "freeze")
                    and getattr(region, event.anomaly_type + "_enabled"))

    def _remove(self, event_id):
        self.active.pop(event_id, None)
        self.acknowledged_at.pop(event_id, None)
        self._pending.discard(event_id)
        self._repeat_due.pop(event_id, None)
        if not self._pending and not self._tests:
            self._first_due = None

    def set_context(self, session_id, running, regions: Iterable[RegionConfig],
                    editing_ids=(), run_id=0):
        changed = (session_id, run_id, bool(running)) != (
            self._session_id, self._run_id, self._running)
        self._session_id, self._run_id = session_id, run_id
        self._running = bool(running)
        # Copy configs: callers may mutate their live region instances in place.
        self._regions = {region.id: replace(region) for region in regions}
        self._editing = set(editing_ids)
        if changed:
            self._tests.clear()
        for event_id, event in list(self.active.items()):
            if changed or not self._eligible(event):
                self._close(event)
                self._remove(event_id)
        if not self._pending and not self._tests:
            self._first_due = None

    def _queue_first(self):
        if self._first_due is None:
            self._first_due = self.clock() + self.aggregate_seconds

    def set_suspended(self, suspended: bool):
        """Pause pending-command attention without ending detector-owned events."""
        suspended = bool(suspended)
        if self._shutdown or suspended == self._suspended:
            return
        self._suspended = suspended
        self._pending.clear()
        self._tests.clear()
        self._first_due = None
        self._repeat_due.clear()
        if not suspended:
            due = self.clock() + self.repeat_seconds
            self._repeat_due = {
                event_id: due for event_id, event in self.active.items()
                if event_id not in self.acknowledged_at and self._eligible(event)
            }

    def ingest(self, events: Iterable[AlarmEvent]) -> list[AcceptedEvent]:
        accepted = []
        if self._shutdown:
            return accepted
        for original in events:
            event = replace(original)
            key = self._key(event)
            if event.anomaly_type == "test":
                if key not in self.closed:
                    self._close(event)
                    if not self._suspended:
                        self._tests[event.id] = event
                        self._queue_first()
                    accepted.append(AcceptedEvent(event, "test"))
                continue
            if event.anomaly_type == "interruption":
                if (key not in self.closed and event.session_id == self._session_id
                        and getattr(event, "run_id", 0) == self._run_id
                        and self._running):
                    self._close(event)
                    accepted.append(AcceptedEvent(event, "interruption"))
                continue
            if event.anomaly_type not in ("black", "freeze", "stream"):
                continue
            if event.ended_at is not None:
                # Ends remain archivable after stop, rule changes, or a new run.
                self._close(event)
                current = self.active.get(event.id)
                if current is not None and self._key(current) == key:
                    self._remove(event.id)
                accepted.append(AcceptedEvent(event, "end"))
                continue
            if key in self.closed or not self._eligible(event):
                continue
            is_new = event.id not in self.active
            self.active[event.id] = event
            if is_new and not self._suspended:
                self._pending.add(event.id)
                self._queue_first()
            # Engine reminder snapshots update facts, never schedule local sound.
            accepted.append(AcceptedEvent(event, "start" if is_new else "update"))
        return accepted

    def acknowledge(self, wall_time=None) -> list[str]:
        acknowledged = [event_id for event_id in self.active
                        if event_id not in self.acknowledged_at]
        stamp = time.time() if wall_time is None else wall_time
        for event_id in acknowledged:
            self.acknowledged_at[event_id] = stamp
            self._pending.discard(event_id)
            self._repeat_due.pop(event_id, None)
        if not self._pending and not self._tests:
            self._first_due = None
        return acknowledged

    def tick(self, now=None) -> list[ReminderBatch]:
        if self._shutdown or self._suspended:
            return []
        now = self.clock() if now is None else now
        batches = []
        if self._first_due is not None and now >= self._first_due:
            events = [event for event_id, event in self.active.items()
                      if event_id in self._pending
                      and event_id not in self.acknowledged_at and self._eligible(event)]
            for event in events:
                self._repeat_due[event.id] = now + self.repeat_seconds
            events.extend(self._tests.values())
            self._pending.clear()
            self._tests.clear()
            self._first_due = None
            if events:
                batches.append(ReminderBatch(tuple(replace(e) for e in events), "first"))
        repeats = [event for event_id, event in self.active.items()
                   if event_id not in self.acknowledged_at and self._eligible(event)
                   and event_id in self._repeat_due and now >= self._repeat_due[event_id]]
        if repeats:
            for event in repeats:
                self._repeat_due[event.id] = now + self.repeat_seconds
            batches.append(ReminderBatch(tuple(replace(e) for e in repeats), "repeat"))
        return batches

    def shutdown(self):
        self._shutdown = True
        self.active.clear()
        self.acknowledged_at.clear()
        self._pending.clear()
        self._tests.clear()
        self._repeat_due.clear()
        self._first_due = None
