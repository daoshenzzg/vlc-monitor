from dataclasses import replace

import numpy as np
import pytest

from vlc_monitor.detection import MonitorEngine
from vlc_monitor.models import AppConfig, FrameSample, RegionConfig


def frame(seq, now, level=100, session="s", size=64):
    pixels = np.full((size, size, 4), level, dtype=np.uint8)
    pixels[:, :, 3] = 255
    pixels.setflags(write=False)
    return FrameSample(session, seq, now, pixels, 1.0)


def setup_engine(**rules):
    region = RegionConfig(id="r", rect=(0, 0, 1, 1), black_count=2,
                          freeze_count=2, recovery_count=2, **rules)
    engine = MonitorEngine(AppConfig(repeat_seconds=5), wall_clock=lambda: 1000)
    engine.start("s", [region], 0)
    return engine, region


@pytest.mark.parametrize("kind,levels,trigger", [
    ("black", [0, 0], 2), ("freeze", [100, 100, 100], 3),
])
def test_new_samples_baseline_trigger_recovery_and_snapshot(kind, levels, trigger):
    engine, _ = setup_engine()
    for seq, level in enumerate(levels, 1):
        events = engine.consume(frame(seq, seq, level), seq)
        assert engine.consume(frame(seq, seq, level), seq) == []
        assert engine.consume(frame(seq + 100, seq, level, session="old"), seq) == []
        if seq < trigger:
            assert not [e for e in events if e.anomaly_type == kind]
    alarm = next(e for e in events if e.anomaly_type == kind)
    assert alarm.hit_count == 2 and alarm.reminder
    assert alarm.started_at == 1000
    same = engine.consume(frame(trigger + 1, trigger + 1, levels[-1]), trigger + 1)
    update = next(e for e in same if e.anomaly_type == kind)
    assert update.id == alarm.id and not update.reminder and alarm.hit_count == 2
    assert update.hit_count == 3
    for offset, level in enumerate((160, 220), 2):
        recovered = engine.consume(frame(trigger + offset, trigger + offset, level), trigger + offset)
    end = next(e for e in recovered if e.id == alarm.id)
    assert end.end_reason == "恢复正常" and not end.reminder
    assert engine.statuses["r"][kind] == "正常"


def test_black_suspends_freeze_and_missing_samples_do_not_recover():
    engine, _ = setup_engine()
    for seq in range(1, 4):
        events = engine.consume(frame(seq, seq), seq)
    frozen = next(e for e in events if e.anomaly_type == "freeze")
    assert engine.tick(3.5) == []
    engine.consume(frame(4, 4, 0), 4)
    assert engine.statuses["r"]["freeze"] == "暂不可判定"
    events = engine.consume(frame(5, 5, 0), 5)
    assert all(e.id != frozen.id for e in events)
    for now in (6, 7, 8, 9, 10):
        events = engine.tick(now)
    timeout = next(e for e in events if e.anomaly_type == "stream")
    assert engine.statuses["r"]["freeze"] == "信号不可判定"
    assert timeout.reminder and engine.tick(11) == []
    for seq in (6, 7, 8):
        events = engine.consume(frame(seq, seq + 6), seq + 6)
        if seq < 8:
            assert not events
    end = next(e for e in events if e.id == timeout.id)
    assert end.end_reason == "恢复正常" and engine.statuses["r"]["freeze"] == "预热"
    assert not any(e.id == frozen.id for e in events)
    engine.consume(frame(9, 15, 160), 15)
    engine.consume(frame(10, 16, 220), 16)
    events = engine.consume(frame(11, 17, 100), 17)
    assert next(e for e in events if e.id == frozen.id).end_reason == "恢复正常"


@pytest.mark.parametrize("action,reason", [
    ("stop", "手动停止"), ("revision", "规则修改"),
    ("delete", "区域删除"), ("disable", "区域停用"), ("session", "会话结束"),
])
def test_explicit_end_is_not_recovery(action, reason):
    engine, region = setup_engine()
    engine.consume(frame(1, 1, 0), 1)
    alarm = engine.consume(frame(2, 2, 0), 2)[0]
    if action == "stop":
        events = engine.stop(3)
    elif action == "revision":
        events = engine.set_regions([replace(region, revision=1)], 3)
    elif action == "delete":
        events = engine.set_regions([], 3)
    elif action == "disable":
        events = engine.set_regions([replace(region, enabled=False)], 3)
    else:
        events = engine.start("new", [region], 3)
        assert engine.consume(frame(100, 4, 0), 4) == []
    end = next(e for e in events if e.id == alarm.id)
    assert end.end_reason == reason and not end.reminder and end.elapsed_seconds == 2


def test_resolution_invalid_roi_and_scheduling_gap_reset_sequences():
    engine, region = setup_engine()
    engine.consume(frame(1, 1), 1)
    engine.consume(frame(2, 2), 2)
    assert not engine.consume(frame(3, 3, size=128), 3)
    assert engine.statuses["r"]["freeze"] == "预热"
    engine.set_regions([replace(region, rect=(0, 0, .1, .1), revision=1)], 4)
    assert not engine.consume(frame(4, 4), 4)
    assert engine.statuses["r"]["black"] == "暂不可判定"
    events = engine.tick(12)
    assert {e.anomaly_type for e in events} == {"interruption", "stream"}
    interruption = next(e for e in events if e.anomaly_type == "interruption")
    assert interruption.end_reason == "监播中断后恢复" and not interruption.reminder


def test_first_frame_timeout_and_reminder_use_monotonic_time():
    engine, _ = setup_engine()
    for now in range(1, 16):
        events = engine.tick(now)
    alarm = events[0]
    assert alarm.anomaly_type == "stream" and engine.stream_status == "等待视频超时"
    for now in range(16, 21):
        events = engine.tick(now)
    assert events[0].id == alarm.id and events[0].reminder
    assert events[0].elapsed_seconds == 5
    stopped = engine.stop(21)
    assert stopped[0].end_reason == "手动停止"
    assert engine.tick(50) == []


def test_decoder_observation_is_independent_of_roi_sampling_and_recovers_once():
    engine, _ = setup_engine()
    assert engine.consume(frame(1, 0), 0) == []
    # ROI sampling can be delayed while the decoder remains continuously fresh.
    for seq in range(2, 122):
        now = (seq - 1) * .05
        assert engine.observe(frame(seq, now), now) == []
        assert engine.tick(now) == []
    assert engine.stream_status == "正常"
    assert engine.statuses["r"]["freeze"] == "预热"
    assert engine.consume(frame(121, 6), 6) == []
    assert engine.statuses["r"]["freeze"] == "疑似"

    # A real gap must not disappear when observe refreshes last_received.
    engine.tick(7)
    events = engine.observe(frame(122, 12), 12)
    stream = next(e for e in events if e.anomaly_type == "stream")
    assert engine.stream_status == "视频恢复确认中"
    assert engine.observe(frame(122, 12), 12) == []
    assert engine.consume(frame(122, 12), 12) == []
    assert engine.observe(frame(123, 12.05), 12.05) == []
    assert engine.consume(frame(123, 12.05), 12.05) == []
    events = engine.observe(frame(124, 12.1), 12.1)
    assert next(e for e in events if e.id == stream.id).end_reason == "恢复正常"
    assert engine.consume(frame(124, 12.1), 12.1) == []
    assert engine.statuses["r"]["freeze"] == "预热"
    assert engine.consume(frame(125, 12.15), 12.15) == []
    assert engine.statuses["r"]["freeze"] == "预热"


def test_ticks_never_repeat_region_alarm_without_new_anomaly_evidence():
    engine, _ = setup_engine()
    engine.consume(frame(1, 1, 0), 1)
    alarm = engine.consume(frame(2, 2, 0), 2)[0]
    for seq in range(3, 9):
        assert engine.observe(frame(seq, seq, 100), seq) == []
        assert engine.tick(seq) == []
    assert engine.consume(frame(8, 8, 100), 8) == []
    assert engine.statuses["r"]["black"] == "恢复确认中"
    repeated = engine.consume(frame(9, 9, 0), 9)
    assert next(e for e in repeated if e.id == alarm.id).reminder


@pytest.mark.parametrize("change", ["revision", "add"])
def test_cached_frame_before_rule_change_cannot_count_for_new_revision(change):
    engine, region = setup_engine()
    region = replace(region, black_count=1)
    engine.set_regions([region], 0)
    engine.consume(frame(1, 0, 100), 0)
    unchanged = replace(region, id="unchanged")
    engine.set_regions([region, unchanged], .5)
    if change == "revision":
        changed = replace(region, revision=1)
    else:
        changed = replace(region, id="added")
    engine.set_regions([changed, unchanged], 1.5)
    events = engine.consume(frame(2, 1, 0), 2)
    assert not any(e.region_id == changed.id for e in events)
    assert any(e.region_id == unchanged.id for e in events)
    assert engine.statuses[changed.id]["black"] == "预热"
    events = engine.consume(frame(3, 2.1, 0), 2.1)
    assert any(e.region_id == changed.id and e.revision == changed.revision for e in events)
