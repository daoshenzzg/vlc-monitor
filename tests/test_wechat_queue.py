from dataclasses import replace

from vlc_monitor.models import AlarmEvent, RegionConfig
from vlc_monitor.notifications.wechat_queue import WechatDeliveryQueue


class Clock:
    now = 0.0

    def __call__(self):
        return self.now


def event(event_id="a", **changes):
    return replace(AlarmEvent(event_id, "session", "r", "Region", "black", 0,
                              100, 100, 100, 4), **changes)


def setup():
    clock = Clock()
    reports = []
    queue = WechatDeliveryQueue(clock, reports.append)
    region = RegionConfig(id="r")
    queue.set_channel(1, 1, True)
    queue.set_context("session", True, [region])
    return queue, clock, reports, region


def test_short_event_merges_first_and_aggregates_with_bounded_safe_text():
    queue, clock, reports, _ = setup()
    queue.ingest(event(region_name="A\nhttps://private/path?token=secret " + "x" * 60), "start")
    queue.ingest(event(ended_at=105, end_reason="恢复正常", region_name="A\nhttps://private/path?token=secret " + "x" * 60), "end")
    assert queue.tick() is None
    clock.now = 2
    task = queue.tick()
    assert task.phase == "start" and task.transient
    assert "恢复正常" in task.text and "private" not in task.text and "secret" not in task.text
    assert queue.tick() is None
    queue.complete(task, "accepted")
    assert queue.tick() is None
    assert reports[-1]["event_ids"] == ("a",)


def test_recovery_cannot_overtake_initial_retry_and_uncertainty_survives():
    queue, clock, reports, _ = setup()
    queue.ingest(event(), "start")
    clock.now = 2
    first = queue.tick()
    queue.ingest(event(ended_at=105, end_reason="恢复正常"), "end")
    queue.complete(first, "uncertain")
    clock.now = 4
    retry = queue.tick()
    assert retry.phase == "start" and retry.attempts == 2 and retry.client_id == first.client_id
    queue.complete(retry, "transient")
    clock.now = 11
    assert queue.tick() is None
    clock.now = 12
    retry = queue.tick()
    assert retry.attempts == 3
    queue.complete(retry, "failed")
    recovery = queue.tick()
    assert recovery.phase == "recovery" and recovery.uncertain
    assert "此前发送结果不确定" in recovery.text
    queue.complete(recovery, "accepted")
    assert all(r["result"] in ("accepted", "uncertain", "failed") for r in reports)


def test_definite_failure_and_expired_initial_allow_one_recovery():
    for result in ("failed", "expired"):
        queue, clock, reports, _ = setup()
        queue.ingest(event(), "start")
        clock.now = 2 if result == "failed" else 300
        task = queue.tick()
        if task:
            queue.complete(task, result)
        assert reports[-1]["result"] == result
        queue.ingest(event(ended_at=105, end_reason="恢复正常"), "end")
        queue.ingest(event(ended_at=105, end_reason="恢复正常"), "end")
        clock.now += 2
        recovery = queue.tick()
        assert recovery.phase == "recovery" and recovery.events[0].id == "a"
        assert "首次告警未确认发送成功" in recovery.text
        queue.complete(recovery, "accepted")
        assert queue.tick() is None


def test_queue_capacity_includes_inflight_and_overflow_is_reported():
    queue, clock, reports, _ = setup()
    for index in range(100):
        queue.ingest(event(str(index)), "start")
    clock.now = 2
    task = queue.tick()
    assert len(task.events) == 8
    queue.ingest(event("overflow"), "start")
    assert reports[-1] == {"event_ids": ("overflow",), "generation": 1, "send_epoch": 1,
                           "phase": "start", "sequence": 0, "attempts": 0, "result": "queue_full"}
    assert queue._states[("session", 0, "overflow")].initial == "failed"
    assert {e.event.id for e in queue._entries} == {str(i) for i in range(8, 100)}
    assert [e.event.id for e in queue._inflight_entries] == [str(i) for i in range(8)]
    queue.complete(task, "accepted")
    queue.ingest(event("overflow", ended_at=105, end_reason="恢复正常"), "end")
    queue.ingest(event("overflow", ended_at=105, end_reason="恢复正常"), "end")
    clock.now += 2
    delivered = list(task.events)
    while (task := queue.tick()) is not None:
        assert len(task.events) <= 8 and len(task.text) <= 1800
        delivered.extend(task.events)
        queue.complete(task, "accepted")
    assert len(delivered) == 101 and {e.id for e in delivered} == {str(i) for i in range(100)} | {"overflow"}
    assert reports[-1]["phase"] == "recovery" and reports[-1]["event_ids"] == ("overflow",)
    assert queue.tick() is None


def test_revision_edit_stamp_and_run_cancel_stale_results():
    queue, clock, reports, region = setup()
    queue.ingest(event(), "start")
    clock.now = 2
    task = queue.tick()
    queue.set_context("session", True, [region], editing_ids=["r"])
    assert queue.inflight is None and not queue.is_current(task)
    queue.complete(task, "accepted")
    assert reports[-1]["result"] == "cancelled"
    queue.set_context("session", True, [region])
    queue.ingest(event(), "start")
    assert queue.tick() is None
    queue.ingest(event("b"), "start")
    region.revision = 1
    queue.set_context("session", True, [region])
    assert queue.pending_count == 0
    queue.ingest(event("c", revision=1), "start")
    queue.set_channel(2, 1, True)
    assert queue.pending_count == 0
    queue.set_context("session", True, [], run_id=1)
    queue.ingest(event("stream", anomaly_type="stream", region_id="", run_id=0), "start")
    assert queue.pending_count == 0
    queue.ingest(event("new-stream", anomaly_type="stream", region_id="", run_id=1), "start")
    assert queue.pending_count == 1


def test_no_catchup_update_or_non_normal_end_and_disabled_by_default():
    queue, clock, _, region = setup()
    queue.set_channel(1, 2, False)
    queue.ingest(event(), "start")
    queue.set_channel(1, 3, True)
    queue.ingest(event(), "update")
    queue.ingest(event(ended_at=105, end_reason="恢复正常"), "end")
    clock.now = 100
    assert queue.tick() is None
    queue.ingest(event("b"), "start")
    queue.ingest(event("b", ended_at=105, end_reason="停止监测"), "end")
    clock.now += 2
    assert queue.tick() is None


def test_legacy_reminder_configuration_and_update_flood_never_send_repeats():
    queue, clock, _, _ = setup()
    queue.configure(True, 60)
    queue.ingest(event(), "start")
    for _ in range(10_000):
        queue.ingest(event(hit_count=100), "update")
        queue.ingest(event(), "start")
    assert queue.pending_count == 1
    assert queue._entries[0].created == 0
    clock.now = 2
    queue.complete(queue.tick(), "accepted")
    for stamp in (100, 160, 162, 1000):
        clock.now = stamp
        queue.configure(True, 60)
        assert queue.tick() is None
    queue.ingest(event(ended_at=105, end_reason="恢复正常"), "end")
    queue.ingest(event(ended_at=105, end_reason="恢复正常"), "end")
    clock.now = 1002
    recovery = queue.tick()
    assert recovery.phase == "recovery"
    queue.complete(recovery, "accepted")
    assert queue.tick() is None
    queue.ingest(event("new-event"), "start")
    clock.now = 1004
    assert queue.tick().events[0].id == "new-event"


def test_retry_after_beyond_ttl_expires_and_session_invalid_pauses():
    queue, clock, reports, _ = setup()
    queue.ingest(event(), "start")
    clock.now = 2
    queue.complete(queue.tick(), "transient", retry_after=1000)
    assert reports[-1]["result"] == "expired" and queue.tick() is None
    queue.ingest(event("b"), "start")
    clock.now = 4
    queue.complete(queue.tick(), "session_invalid")
    queue.ingest(event("c"), "start")
    clock.now += 10
    assert queue.tick() is None


def test_updates_do_not_mutate_snapshots_and_full_queue_rejects_correct_identity():
    queue, clock, reports, _ = setup()
    queue.ingest(event(), "start")
    queue.ingest(event(hit_count=12), "update")
    queue.ingest(event("b"), "start")
    clock.now = 2
    task = queue.tick()
    assert [(e.id, e.hit_count) for e in task.events] == [("a", 4), ("b", 4)]
    queue.complete(task, "accepted")
    queue.configure(True, 60)
    clock.now = 62
    assert queue.tick() is None
    for index in range(100):
        queue.ingest(event("new" + str(index)), "start")
    assert queue.pending_count == 100
    queue.ingest(event("priority"), "start")
    assert queue.pending_count == 100
    assert reports[-1]["phase"] == "start" and reports[-1]["result"] == "queue_full"
    assert reports[-1]["event_ids"] == ("priority",)
    assert [e.event.id for e in queue._entries] == ["new" + str(i) for i in range(100)]
    queue.ingest(event("priority", ended_at=105, end_reason="恢复正常"), "end")
    assert reports[-1]["phase"] == "recovery" and reports[-1]["event_ids"] == ("priority",)
    assert reports[-1]["result"] == "queue_full"


def test_first_and_recovery_each_have_only_two_extra_retries():
    queue, clock, reports, _ = setup()
    queue.ingest(event(), "start")
    for stamp, attempt in ((2, 1), (4, 2), (12, 3)):
        clock.now = stamp
        task = queue.tick()
        assert task.attempts == attempt
        queue.complete(task, "transient")
    queue.ingest(event(ended_at=105, end_reason="恢复正常"), "end")
    client_id = None
    for stamp, attempt in ((14, 1), (16, 2), (24, 3)):
        clock.now = stamp
        task = queue.tick()
        assert task.phase == "recovery" and task.attempts == attempt
        client_id = client_id or task.client_id
        assert task.client_id == client_id
        assert "首次告警未确认发送成功" in task.text
        queue.complete(task, "transient")
    clock.now = 1000
    assert queue.tick() is None and queue.pending_count == 0
    assert len(reports) == 6


def test_delayed_mailbox_ingress_expires_initial_but_not_fresh_recovery():
    queue, clock, reports, _ = setup()
    clock.now = 301
    queue.ingest(event(), "start", created_at=0)
    assert reports[-1]["result"] == "expired"
    queue.ingest(event(ended_at=105, end_reason="恢复正常"), "end", created_at=300)
    assert queue.pending_count == 1 and queue.tick() is None
    clock.now = 302
    recovery = queue.tick()
    assert recovery.phase == "recovery" and "首次告警未确认发送成功" in recovery.text
    queue.complete(recovery, "accepted")
    assert len(reports) == 2


def test_ingress_aggregation_and_recovery_use_their_own_timestamps():
    queue, clock, reports, _ = setup()
    clock.now = 10
    queue.ingest(event(), "start", created_at=0)
    first = queue.tick()  # Already outside ingress aggregation window.
    assert first is not None
    queue.complete(first, "accepted")
    clock.now = 310
    queue.ingest(event(ended_at=105, end_reason="恢复正常"), "end", created_at=309)
    assert queue.tick() is None
    clock.now = 311
    recovery = queue.tick()
    assert recovery.phase == "recovery"
    queue.complete(recovery, "accepted")
    queue.ingest(event("b"), "start", created_at=309)
    queue.complete(queue.tick(), "accepted")
    clock.now = 1000
    queue.ingest(event("b", ended_at=105, end_reason="恢复正常"), "end", created_at=600)
    assert reports[-1]["phase"] == "recovery" and reports[-1]["result"] == "expired"
    assert queue.tick() is None


def test_batched_message_size_and_retry_keep_bounded_membership():
    queue, clock, _, _ = setup()
    for index in range(20):
        queue.ingest(event(str(index), region_name="区域" * 40, hit_count=10 ** 200), "start")
    clock.now = 2
    first = queue.tick()
    assert len(first.events) == 8 and len(first.text) <= 1800
    queue.complete(first, "uncertain")
    clock.now = 4
    # New initial work may run before the retry, but each request stays bounded.
    tasks = []
    while (task := queue.tick()) is not None:
        assert len(task.events) <= 8 and len(task.text) <= 1800
        tasks.append(task)
        queue.complete(task, "accepted")
    retry = next(task for task in tasks if task.attempts == 2)
    assert retry.client_id == first.client_id
    assert [e.id for e in retry.events] == [e.id for e in first.events]


def test_state_limit_evicts_ended_event_not_live_initial_needed_for_recovery():
    queue, clock, reports, _ = setup()
    queue.STATE_LIMIT = 2
    queue.ingest(event("a"), "start")
    clock.now = 2
    queue.complete(queue.tick(), "accepted")
    for index, name in enumerate(("b", "c"), start=1):
        queue.ingest(event(name), "start")
        queue.ingest(event(name, ended_at=105, end_reason="恢复正常"), "end")
        clock.now += 2
        task = queue.tick()
        assert task.transient and task.events[0].id == name
        queue.complete(task, "accepted")
    queue.ingest(event("a", ended_at=110, end_reason="恢复正常"), "end")
    clock.now += 2
    task = queue.tick()
    assert task.phase == "recovery" and task.events[0].id == "a"
    assert not task.uncertain
    queue.complete(task, "accepted")
    assert len(queue._states) <= 2 and len(queue._closed) <= 2


def test_all_live_state_capacity_rejects_new_initial_and_bounds_tombstones():
    queue, clock, reports, _ = setup()
    queue.STATE_LIMIT = 2
    for name in ("a", "b"):
        queue.ingest(event(name), "start")
    clock.now = 2
    queue.complete(queue.tick(), "accepted")
    for name in ("c", "d", "e"):
        queue.ingest(event(name), "start")
        assert reports[-1]["event_ids"] == (name,)
        assert reports[-1]["result"] == "queue_full"
        queue.ingest(event(name, ended_at=105, end_reason="恢复正常"), "end")
    assert len(queue._states) == 2 and len(queue._closed) == 2
    queue.ingest(event("e"), "start")  # Recent rejected ID remains deduplicated.
    assert len(reports) == 4
    queue.ingest(event("a", ended_at=110, end_reason="恢复正常"), "end")
    clock.now += 2
    recovery = queue.tick()
    assert recovery.phase == "recovery" and recovery.events[0].id == "a"


def test_cancelled_aggregate_initial_preserves_survivor_recovery_without_repeat():
    queue, clock, reports, region = setup()
    second_region = RegionConfig(id="r2")
    queue.set_context("session", True, [region, second_region])
    queue.configure(True, 60)
    queue.ingest(event("a"), "start")
    queue.ingest(event("b", region_id="r2"), "start")
    clock.now = 2
    first = queue.tick()
    assert len(first.events) == 2
    queue.set_context("session", True, [region, second_region], editing_ids=["r"])
    assert not queue.is_current(first)
    queue.complete(first, "accepted")
    clock.now = 4
    assert queue.tick() is None and queue.pending_count == 0
    clock.now = 61.99
    assert queue.tick() is None and queue.pending_count == 0
    clock.now = 62
    assert queue.tick() is None and queue.pending_count == 0
    queue.ingest(event("b", region_id="r2", ended_at=110, end_reason="恢复正常"), "end")
    clock.now = 64
    recovery = queue.tick()
    assert recovery.phase == "recovery" and [e.id for e in recovery.events] == ["b"]
    assert recovery.uncertain


def test_non_normal_end_cancels_inflight_initial_and_late_failure_cannot_retry():
    queue, clock, reports, _ = setup()
    queue.ingest(event(), "start")
    clock.now = 2
    initial = queue.tick()
    queue.ingest(event(ended_at=105, end_reason="停止监测"), "end")
    assert not queue.is_current(initial)
    assert reports[-1]["phase"] == "start" and reports[-1]["result"] == "cancelled"
    queue.complete(initial, "transient")
    clock.now = 1000
    assert queue.tick() is None and queue.pending_count == 0


def test_duplicate_start_does_not_refresh_retry_budget_or_ttl():
    queue, clock, reports, _ = setup()
    queue.ingest(event(), "start")
    clock.now = 290
    first = queue.tick()
    queue.complete(first, "transient", retry_after=9)
    clock.now = 298
    queue.ingest(event(hit_count=10), "start", created_at=298)
    assert queue._entries[0].created == 0 and queue._entries[0].attempts == 1
    assert queue.tick() is None
    clock.now = 299
    retry = queue.tick()
    assert retry.client_id == first.client_id and retry.attempts == 2
    queue.complete(retry, "transient")
    assert reports[-1]["result"] == "expired"
    clock.now = 300
    queue.ingest(event(), "start")
    assert queue.tick() is None
    queue.ingest(event(ended_at=105, end_reason="恢复正常"), "end")
    clock.now = 302
    assert queue.tick().phase == "recovery"


def test_old_generation_and_run_results_cannot_change_new_initial():
    queue, clock, reports, region = setup()
    queue.ingest(event(), "start")
    clock.now = 2
    old = queue.tick()
    queue.set_channel(2, 2, True)
    queue.set_context("session", True, [region], run_id=1)
    queue.ingest(event(ended_at=105, end_reason="恢复正常"), "end")
    queue.ingest(event(run_id=1), "start")
    clock.now = 4
    current = queue.tick()
    count = len(reports)
    queue.complete(old, "uncertain")
    assert len(reports) == count and queue.is_current(current)
    assert current.generation == 2 and current.send_epoch == 2
    queue.complete(current, "accepted")
    queue.ingest(event(run_id=1, ended_at=105, end_reason="恢复正常"), "end")
    clock.now = 6
    recovery = queue.tick()
    assert recovery.phase == "recovery" and not recovery.uncertain
