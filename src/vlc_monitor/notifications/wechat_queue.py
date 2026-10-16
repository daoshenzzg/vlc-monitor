"""Bounded, single-owner WeChat policy; no transport, credentials or UI state."""
from collections import OrderedDict
from dataclasses import dataclass, replace
import hashlib
import math
import re
import time
import unicodedata
import uuid

from vlc_monitor.models import AlarmEvent


@dataclass(frozen=True)
class DeliveryTask:
    events: tuple[AlarmEvent, ...]
    phase: str
    sequence: int
    generation: int
    send_epoch: int
    attempts: int
    client_id: str
    text: str
    uncertain: bool = False
    transient: bool = False


@dataclass
class _State:
    event: AlarmEvent
    initial: str = "pending"
    uncertain: bool = False
    ended: bool = False


@dataclass
class _Entry:
    event: AlarmEvent
    phase: str
    sequence: int
    created: float
    due: float
    attempts: int = 0
    client_id: str = ""
    uncertain: bool = False
    transient: bool = False
    initial_failed: bool = False


def _safe_name(name):
    name = "".join(c for c in str(name)[:512] if not unicodedata.category(c).startswith("C"))
    name = re.sub(r"(?i)(?:[a-z][a-z0-9+.-]*://|www\.)\S+", "[redacted]", name)
    name = re.sub(r"(?i)\b(?:token|secret|password|authorization|context_token)\s*[:=]\s*\S+", "[redacted]", name)
    name = re.sub(r"[A-Za-z0-9_+/=.-]{16,}", "[redacted]", name)
    name = re.sub(r"\b(?:\d{1,3}\.){3}\d{1,3}(?::\d+)?\b", "[redacted]", name)
    name = re.sub(r"(?i)\b[\w.+-]+@[\w.-]+\.[a-z]{2,}\b", "[redacted]", name)
    return " ".join(name.split())[:40] or "区域"


class WechatDeliveryQueue:
    MAX_ENTRIES = 100
    MAX_BATCH = 8
    MAX_TEXT = 1800
    STATE_LIMIT = 2048
    TTL = 300.0
    AGGREGATE = 2.0

    def __init__(self, clock=time.monotonic, report=lambda record: None):
        self.clock, self.report = clock, report
        self._states = OrderedDict()
        self._closed = OrderedDict()
        self._entries = []
        self._inflight = None
        self._inflight_entries = ()
        self._generation = self._send_epoch = 0
        self._enabled = self._running = False
        self._session_id, self._run_id = "", 0
        self._regions, self._editing = {}, set()

    @property
    def pending_count(self):
        return len(self._entries) + len(self._inflight_entries)

    @property
    def inflight(self):
        return self._inflight

    def is_current(self, task):
        return task is self._inflight and task is not None

    @staticmethod
    def _key(event):
        return event.session_id, event.run_id, event.id

    def _close_key(self, key):
        self._closed[key] = None
        self._closed.move_to_end(key)
        while len(self._closed) > self.STATE_LIMIT:
            self._closed.popitem(last=False)

    def _eligible(self, event):
        if (not self._enabled or not self._running or event.session_id != self._session_id
                or event.run_id != self._run_id):
            return False
        if event.anomaly_type == "stream":
            return True
        region = self._regions.get(event.region_id)
        return bool(region and region.enabled and region.id not in self._editing
                    and region.revision == event.revision
                    and event.anomaly_type in ("black", "freeze")
                    and getattr(region, event.anomaly_type + "_enabled"))

    def _record(self, entries, result, *, task=None):
        if not entries:
            return
        first = entries[0]
        self.report({"event_ids": tuple(e.event.id for e in entries),
                     "generation": task.generation if task else self._generation,
                     "send_epoch": task.send_epoch if task else self._send_epoch,
                     "phase": first.phase, "sequence": first.sequence,
                     "attempts": first.attempts, "result": result})

    def _drop(self, entry, result):
        self._entries.remove(entry)
        state = self._states.get(self._key(entry.event))
        if state and entry.phase == "start":
            state.initial = "uncertain" if state.uncertain else "failed"
        self._record([entry], result)

    def _cancel(self, keys=None):
        for entry in list(self._entries):
            if keys is None or self._key(entry.event) in keys:
                self._drop(entry, "cancelled")
        if self._inflight and (keys is None or any(self._key(e.event) in keys for e in self._inflight_entries)):
            # A batch is one request: cancel it atomically, never re-send survivors.
            self._record(self._inflight_entries, "cancelled", task=self._inflight)
            for entry in self._inflight_entries:
                state = self._states.get(self._key(entry.event))
                if state and entry.phase == "start":
                    state.initial, state.uncertain = "uncertain", True
            self._inflight, self._inflight_entries = None, ()

    def set_channel(self, generation, send_epoch, enabled):
        stamp = generation, send_epoch, bool(enabled)
        if stamp != (self._generation, self._send_epoch, self._enabled):
            self._cancel()
            self._states.clear()
            self._closed.clear()
            self._generation, self._send_epoch, self._enabled = stamp

    def set_context(self, session_id, running, regions, editing_ids=(), run_id=0):
        changed = (session_id, bool(running), run_id) != (self._session_id, self._running, self._run_id)
        self._session_id, self._running, self._run_id = session_id, bool(running), run_id
        self._regions = {r.id: replace(r) for r in regions}
        self._editing = set(editing_ids)
        invalid = {key for key, state in self._states.items() if changed or not self._eligible(state.event)}
        self._cancel(invalid)
        # Tombstones prevent update/end snapshots from resurrecting invalid events.
        for key in invalid:
            self._states[key].ended = True

    def configure(self, reminders_enabled=False, repeat_seconds=60):
        """Accept legacy settings without enabling periodic WeChat messages."""

    def _enqueue(self, state, phase, sequence=0, created_at=None):
        now = self.clock()
        for pending in list(self._entries):
            if now - pending.created >= self.TTL:
                self._drop(pending, "expired")
        created = now if created_at is None else min(now, created_at)
        entry = _Entry(replace(state.event), phase, sequence, created, created + self.AGGREGATE,
                       uncertain=state.uncertain)
        if now - created >= self.TTL:
            self._record([entry], "expired")
            if phase == "start":
                state.initial = "failed"
            return
        if len(self._entries) + len(self._inflight_entries) >= self.MAX_ENTRIES:
            self._record([entry], "queue_full")
            if phase == "start":
                state.initial = "failed"
            return
        self._entries.append(entry)

    def ingest(self, event, phase, created_at=None):
        if phase not in ("start", "end"):
            return
        if created_at is not None and (not isinstance(created_at, (int, float)) or not math.isfinite(created_at)):
            raise ValueError("Ingress timestamp must be finite monotonic seconds")
        if not self._eligible(event):
            return
        for entry in list(self._entries):
            if self.clock() - entry.created >= self.TTL:
                self._drop(entry, "expired")
        event = replace(event)
        key = self._key(event)
        if key in self._closed:
            return
        state = self._states.get(key)
        if state is None:
            if phase != "start" or event.ended_at is not None:
                return
            if len(self._states) >= self.STATE_LIMIT:
                pending_keys = {self._key(e.event) for e in (*self._entries, *self._inflight_entries)}
                oldest = next((key for key, state in self._states.items()
                               if state.ended and key not in pending_keys), None)
                if oldest is None:
                    now = self.clock()
                    rejected = _Entry(event, "start", 0, now, now)
                    self._record([rejected], "queue_full")
                    self._close_key(key)
                    return
                self._cancel({oldest})
                self._states.pop(oldest)
                self._close_key(oldest)
            state = _State(event)
            self._states[key] = state
            self._enqueue(state, "start", created_at=created_at)
            return
        if state.ended:
            return
        state.event = event
        for entry in self._entries:
            if self._key(entry.event) == key:
                entry.event = replace(event)
        if phase != "end":
            return
        state.ended = True
        if event.end_reason != "恢复正常":
            self._cancel({key})
            return
        initial = next((e for e in self._entries if self._key(e.event) == key and e.phase == "start"), None)
        if initial and initial.attempts == 0:
            initial.transient = True
        else:
            self._enqueue(state, "recovery", created_at=created_at)

    def tick(self):
        now = self.clock()
        for entry in list(self._entries):
            if not self._eligible(entry.event):
                self._drop(entry, "cancelled")
            elif now - entry.created >= self.TTL:
                self._drop(entry, "expired")
        if self._inflight or not self._enabled:
            return None
        ready = []
        for entry in list(self._entries):
            state = self._states.get(self._key(entry.event))
            if entry.due > now or (entry.phase == "recovery" and state and state.initial == "pending"):
                continue
            if entry.phase == "recovery" and state:
                entry.uncertain = state.uncertain
                entry.initial_failed = state.initial == "failed"
            ready.append(entry)
        if not ready:
            return None
        first = ready[0]
        candidates = [e for e in ready if (e.phase, e.sequence, e.attempts, e.client_id, e.transient, e.uncertain, e.initial_failed)
                 == (first.phase, first.sequence, first.attempts, first.client_id, first.transient, first.uncertain, first.initial_failed)]
        batch = []
        for entry in candidates[:self.MAX_BATCH]:
            proposed = batch + [entry]
            if len(self._format(proposed, any(e.uncertain for e in proposed))) > self.MAX_TEXT:
                break
            batch.append(entry)
        client_id = first.client_id or uuid.uuid4().hex
        for entry in batch:
            self._entries.remove(entry)
            entry.attempts += 1
            entry.client_id = client_id
        uncertain = any(e.uncertain for e in batch)
        text = self._format(batch, uncertain)
        task = DeliveryTask(tuple(replace(e.event) for e in batch), first.phase, first.sequence,
                            self._generation, self._send_epoch, first.attempts, client_id,
                            text, uncertain, first.transient)
        self._inflight, self._inflight_entries = task, tuple(batch)
        return task

    @staticmethod
    def _format(entries, uncertain):
        labels = {"start": "首次告警", "recovery": "恢复正常"}
        lines = [labels[entries[0].phase]]
        if entries[0].transient:
            lines.append("短暂异常，现已恢复正常")
        if entries[0].initial_failed:
            lines.append("首次告警未确认发送成功")
        if uncertain:
            lines.append("此前发送结果不确定；有限重试可能重复，不代表已送达")
        kinds = {"black": "黑屏", "freeze": "画面冻结", "stream": "视频中断"}
        for entry in entries:
            event = entry.event
            short_id = hashlib.sha256(event.id.encode()).hexdigest()[:8]
            duration = max(0, min(float(event.elapsed_seconds), 1e9))
            stamp = time.strftime("%m-%d %H:%M:%S", time.localtime(max(0, min(event.triggered_at, 2147483647))))
            hits = max(0, min(event.hit_count, 1_000_000_000))
            lines.append(f"{_safe_name(event.region_name)} | {kinds[event.anomaly_type]} | {hits} 次 / {duration:.0f} 秒 | {stamp} | #{short_id}")
        return "\n".join(lines)

    def complete(self, task, result, retry_after=0):
        if task is not self._inflight:
            return
        if result not in ("accepted", "failed", "uncertain", "transient", "session_invalid"):
            raise ValueError("Unknown delivery result")
        entries = self._inflight_entries
        self._inflight, self._inflight_entries = None, ()
        self._record(entries, result if result in ("accepted", "uncertain") else "failed", task=task)
        now = self.clock()
        for entry in entries:
            state = self._states.get(self._key(entry.event))
            if not state:
                continue
            if result == "uncertain":
                state.uncertain = entry.uncertain = True
            retry = result in ("transient", "uncertain") and entry.attempts < 3
            if retry and self._eligible(entry.event):
                delay = (2, 8)[entry.attempts - 1]
                if isinstance(retry_after, (int, float)) and math.isfinite(retry_after):
                    delay = max(delay, retry_after)
                entry.due = min(now + delay, entry.created + self.TTL)
                if entry.due < entry.created + self.TTL:
                    self._entries.append(entry)
                    continue
                self._record([entry], "expired")
            if entry.phase == "start":
                state.initial = "accepted" if result == "accepted" else "uncertain" if state.uncertain else "failed"
        if result == "session_invalid":
            self._cancel()
            self._states.clear()
            self._enabled = False
