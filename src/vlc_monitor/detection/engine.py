"""Pure image rules and a single-owner, clock-injected monitoring state machine."""

from copy import deepcopy
from dataclasses import dataclass, field, replace
import math
import time
import uuid

import cv2
import numpy as np

from vlc_monitor.models import AlarmEvent, AppConfig, DetectionResult, FrameSample, RegionConfig
from vlc_monitor.ui.geometry import pixel_bounds


class Detector:
    """Preprocess one ROI; state and sample freshness belong to MonitorEngine."""

    @staticmethod
    def prepare(sample: FrameSample, region: RegionConfig) -> np.ndarray:
        region.validate()
        pixels = sample.pixels
        if pixels.ndim != 3 or pixels.shape[2] != 4 or pixels.dtype != np.uint8:
            raise ValueError("视频帧必须为 BGRA uint8")
        left, top, right, bottom = pixel_bounds(region.rect, sample.width, sample.height)
        if right - left < 16 or bottom - top < 16:
            raise ValueError("区域必须至少为 16×16 视频像素")
        gray = cv2.cvtColor(pixels[top:bottom, left:right], cv2.COLOR_BGRA2GRAY)
        if gray.shape[1] > 320:
            height = max(1, round(gray.shape[0] * 320 / gray.shape[1]))
            gray = cv2.resize(gray, (320, height), interpolation=cv2.INTER_AREA)
        return gray

    @staticmethod
    def black(gray: np.ndarray, region: RegionConfig) -> float:
        return float(np.mean(gray < region.black_level))

    @staticmethod
    def smooth(gray: np.ndarray) -> np.ndarray:
        return cv2.GaussianBlur(gray, (3, 3), 0)

    @staticmethod
    def freeze(previous: np.ndarray, current: np.ndarray, region: RegionConfig) -> float:
        return float(np.mean(cv2.absdiff(previous, current) < region.freeze_delta))


@dataclass
class _Rule:
    hits: int = 0
    recovering: int = 0
    first_mono: float | None = None
    first_wall: float | None = None
    event: AlarmEvent | None = None
    last_reminder: float = 0.0


@dataclass
class _Region:
    config: RegionConfig
    valid_after: float
    baseline: np.ndarray | None = None
    rules: dict[str, _Rule] = field(default_factory=lambda: {"black": _Rule(), "freeze": _Rule()})


class MonitorEngine:
    """Call on one worker thread. Returned events are independent upsert snapshots."""

    def __init__(self, config: AppConfig, wall_clock=time.time):
        self.config = deepcopy(config)
        self.wall_clock = wall_clock
        self.running = False
        self.statuses: dict[str, dict[str, str]] = {}
        self.stream_status = "未监播"
        self.last_results: list[DetectionResult] = []
        self._regions: dict[str, _Region] = {}
        self._stream: AlarmEvent | None = None

    def start(self, session_id: str, regions: list[RegionConfig], now: float) -> list[AlarmEvent]:
        if not session_id or not any(r.enabled for r in regions):
            raise ValueError("开始监播需要有效会话和启用区域")
        for region in regions:
            region.validate()
        if len({r.id for r in regions}) != len(regions):
            raise ValueError("区域 ID 重复")
        events = self.stop(now, "会话结束") if self.running else []
        self.running = True
        self._session = session_id
        self._started = now
        self._last_tick = now
        self._last_received = None
        self._last_seq = -1
        self._observed_seq = -1
        self._roi_after_seq = -1
        self._size = None
        self._stream = None
        self._stream_start = None
        self._stream_reminder = now
        self._stream_recovery = 0
        self._regions = {r.id: _Region(deepcopy(r), now) for r in regions if r.enabled}
        self.statuses = {r.id: self._initial_status(r) for r in regions}
        self.stream_status = "等待视频"
        self.last_results = []
        return events

    @staticmethod
    def _initial_status(region):
        return {kind: "预热" if region.enabled and getattr(region, kind + "_enabled") else "已停用"
                for kind in ("black", "freeze")}

    def _finish(self, event, now, reason, started):
        event.ended_at = self.wall_clock()
        event.end_reason = reason
        event.elapsed_seconds = max(0.0, now - started)
        event.reminder = False
        return replace(event)

    def stop(self, now: float, reason: str = "手动停止") -> list[AlarmEvent]:
        if not self.running:
            return []
        events = []
        for state in self._regions.values():
            events.extend(self._end_region(state, now, reason))
        if self._stream:
            events.append(self._finish(self._stream, now, reason, self._stream_start))
            self._stream = None
        self.running = False
        self.stream_status = reason
        for statuses in self.statuses.values():
            for kind in statuses:
                statuses[kind] = "已停止"
        return events

    def _end_region(self, state, now, reason):
        events = []
        for rule in state.rules.values():
            if rule.event:
                events.append(self._finish(rule.event, now, reason, rule.first_mono))
                rule.event = None
        return events

    def set_regions(self, regions: list[RegionConfig], now: float, reason: str = "规则修改") -> list[AlarmEvent]:
        for region in regions:
            region.validate()
        if len({r.id for r in regions}) != len(regions):
            raise ValueError("区域 ID 重复")
        incoming = {r.id: r for r in regions}
        events = []
        for rid, state in list(self._regions.items()):
            new = incoming.get(rid)
            if new != state.config:
                why = "区域删除" if new is None else "区域停用" if not new.enabled else reason
                events.extend(self._end_region(state, now, why))
                del self._regions[rid]
        for region in regions:
            if region.enabled and region.id not in self._regions:
                self._regions[region.id] = _Region(deepcopy(region), now)
                self.statuses[region.id] = self._initial_status(region)
            elif not region.enabled:
                self.statuses[region.id] = self._initial_status(region)
        self.statuses = {rid: value for rid, value in self.statuses.items() if rid in incoming}
        return events

    def _interrupt(self, status="暂不可判定"):
        for rid, state in self._regions.items():
            state.baseline = None
            for kind, rule in state.rules.items():
                rule.hits = rule.recovering = 0
                if rule.event is None:
                    rule.first_mono = rule.first_wall = None
                if getattr(state.config, kind + "_enabled"):
                    self.statuses[rid][kind] = status

    def _new_event(self, kind, now, region=None, first_wall=None):
        wall = self.wall_clock()
        return AlarmEvent(str(uuid.uuid4()), self._session, region.id if region else "",
                          region.name if region else "视频流", kind, region.revision if region else 0,
                          wall if first_wall is None else first_wall, wall, wall, 1)

    def tick(self, now: float) -> list[AlarmEvent]:
        return self._tick(now)

    def _tick(self, now: float, timeout_at: float | None = None) -> list[AlarmEvent]:
        if not self.running:
            return []
        events = []
        if now - self._last_tick > self.config.update_timeout:
            event = self._new_event("interruption", now)
            event.ended_at = event.last_seen
            event.end_reason = "监播中断后恢复"
            event.elapsed_seconds = now - self._last_tick
            events.append(event)
            self._interrupt()
            self._stream_recovery = 0
        self._last_tick = now
        limit = self.config.first_frame_timeout if self._last_received is None else self.config.update_timeout
        last = self._started if self._last_received is None else self._last_received
        if (now if timeout_at is None else timeout_at) - last >= limit:
            self._stream_recovery = 0
            self.stream_status = "等待视频超时" if self._last_received is None else "视频更新超时"
            self._interrupt("信号不可判定")
            if self._stream is None:
                self._stream = self._new_event("stream", now)
                self._stream_start = now
                self._stream_reminder = now
                self._stream.reminder = True
                events.append(replace(self._stream))
            elif now - self._stream_reminder >= self.config.repeat_seconds:
                self._stream_reminder = now
                self._stream.last_seen = self.wall_clock()
                self._stream.elapsed_seconds = now - self._stream_start
                events.append(replace(self._stream, reminder=True))
        return events

    def observe(self, sample: FrameSample, now: float) -> list[AlarmEvent]:
        """Observe decoder freshness at poll cadence without running ROI rules."""
        if not self.running or sample.session_id != self._session or sample.frame_seq <= self._observed_seq:
            return []
        if (not math.isfinite(sample.received_at) or sample.received_at > now
                or sample.received_at < self._started
                or now - sample.received_at >= self.config.update_timeout):
            self._interrupt()
            self._stream_recovery = 0
            return self.tick(now)
        if self._last_received is not None and sample.received_at < self._last_received:
            return []
        try:
            valid = (sample.pixels.ndim == 3 and sample.pixels.shape[2] == 4
                     and sample.pixels.dtype == np.uint8 and sample.width > 0 and sample.height > 0)
        except (AttributeError, IndexError):
            valid = False
        if not valid:
            self._interrupt()
            self._stream_recovery = 0
            return self.tick(now)
        # Detect real decoder gaps before replacing freshness, but do not confuse
        # time spent waiting for the next ROI sampling deadline with a frame gap.
        events = self._tick(now, timeout_at=sample.received_at)
        self._observed_seq = sample.frame_seq
        self._last_received = sample.received_at
        if self._stream:
            self._stream_recovery += 1
            self.stream_status = "视频恢复确认中"
            if self._stream_recovery < 3:
                return events
            events.append(self._finish(self._stream, now, "恢复正常", self._stream_start))
            self._stream = None
            self._interrupt("预热")
            self.stream_status = "正常"
            self._size = (sample.width, sample.height)
            self._roi_after_seq = sample.frame_seq
            return events
        self.stream_status = "正常"
        return events

    def consume(self, sample: FrameSample, now: float) -> list[AlarmEvent]:
        self.last_results = []
        if not self.running or sample.session_id != self._session or sample.frame_seq <= self._last_seq:
            return []
        events = self.observe(sample, now)
        if (sample.frame_seq != self._observed_seq or self._stream is not None
                or sample.frame_seq <= self._roi_after_seq
                or now - sample.received_at >= self.config.update_timeout):
            return events
        self._last_seq = sample.frame_seq
        size = (sample.width, sample.height)
        if self._size is not None and self._size != size:
            self._interrupt("预热")
        self._size = size
        for rid, state in self._regions.items():
            region = state.config
            if sample.received_at < state.valid_after:
                continue
            try:
                gray = Detector.prepare(sample, region)
            except (ValueError, cv2.error):
                state.baseline = None
                for kind, rule in state.rules.items():
                    rule.hits = rule.recovering = 0
                    if rule.event is None:
                        rule.first_mono = rule.first_wall = None
                    if getattr(region, kind + "_enabled"):
                        self.statuses[rid][kind] = "暂不可判定"
                    self.last_results.append(DetectionResult(rid, kind, False, None, sample.frame_seq, region.revision, None))
                continue
            black_score = Detector.black(gray, region)
            is_black = black_score >= region.black_ratio
            if region.black_enabled:
                events.extend(self._apply(state, "black", is_black, now))
                self.last_results.append(DetectionResult(rid, "black", True, black_score, sample.frame_seq, region.revision, is_black))
            if not region.freeze_enabled:
                continue
            smooth = Detector.smooth(gray)
            if is_black or state.baseline is None:
                state.baseline = None if is_black else smooth
                rule = state.rules["freeze"]
                rule.hits = rule.recovering = 0
                if rule.event is None:
                    rule.first_mono = rule.first_wall = None
                self.statuses[rid]["freeze"] = "暂不可判定" if is_black or rule.event else "预热"
                self.last_results.append(DetectionResult(rid, "freeze", False, None, sample.frame_seq, region.revision, None))
            else:
                score = Detector.freeze(state.baseline, smooth, region)
                state.baseline = smooth
                events.extend(self._apply(state, "freeze", score >= region.freeze_ratio, now))
                self.last_results.append(DetectionResult(rid, "freeze", True, score, sample.frame_seq, region.revision, score >= region.freeze_ratio))
        return events

    def _apply(self, state, kind, hit, now):
        region = state.config
        rule = state.rules[kind]
        events = []
        if hit:
            rule.recovering = 0
            if rule.first_mono is None:
                rule.first_mono, rule.first_wall = now, self.wall_clock()
            rule.hits += 1
            if rule.event is None and rule.hits >= getattr(region, kind + "_count"):
                rule.event = self._new_event(kind, now, region, rule.first_wall)
                rule.event.hit_count = rule.hits - 1
                rule.last_reminder = now
                rule.event.reminder = True
            if rule.event:
                event = rule.event
                event.hit_count += 1
                if now - rule.last_reminder >= self.config.repeat_seconds:
                    rule.last_reminder = now
                    event.reminder = True
                event.last_seen = self.wall_clock()
                event.elapsed_seconds = now - rule.first_mono
                events.append(replace(event))
                event.reminder = False
                self.statuses[region.id][kind] = "告警中"
            else:
                self.statuses[region.id][kind] = "疑似"
        else:
            rule.hits = 0
            if rule.event:
                rule.recovering += 1
                self.statuses[region.id][kind] = "恢复确认中"
                if rule.recovering >= region.recovery_count:
                    events.append(self._finish(rule.event, now, "恢复正常", rule.first_mono))
                    rule.event = None
                    rule.first_mono = rule.first_wall = None
                    rule.recovering = 0
                    self.statuses[region.id][kind] = "正常"
            else:
                rule.first_mono = rule.first_wall = None
                self.statuses[region.id][kind] = "正常"
        return events
