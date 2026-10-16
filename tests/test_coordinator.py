from dataclasses import replace

from vlc_monitor.models import AlarmEvent, RegionConfig
from vlc_monitor.notifications.coordinator import AlarmCoordinator


class Clock:
    now = 0.0

    def __call__(self):
        return self.now


def event(event_id="a", **changes):
    return replace(AlarmEvent(event_id, "session", "r", "Region", "black", 0,
                              100, 100, 100, 4), **changes)


def setup():
    clock = Clock()
    coordinator = AlarmCoordinator(clock=clock)
    region = RegionConfig(id="r")
    coordinator.set_context("session", True, [region])
    return coordinator, clock, region


def test_first_aggregation_updates_and_independent_repeat_schedule():
    coordinator, clock, _ = setup()
    assert coordinator.ingest([event()])[0].phase == "start"
    clock.now = 0.2
    coordinator.ingest([event("b"), event(hit_count=8, reminder=True)])
    assert coordinator.tick() == []
    clock.now = 0.3
    first, = coordinator.tick()
    assert first.phase == "first"
    assert [(e.id, e.hit_count) for e in first.events] == [("a", 8), ("b", 4)]
    coordinator.ingest([event(reminder=True)])
    clock.now = 5.29
    assert coordinator.tick() == []
    clock.now = 5.3
    repeat, = coordinator.tick()
    assert repeat.phase == "repeat" and len(repeat.events) == 2
    clock.now = 100
    assert len(coordinator.tick()) == 1
    assert coordinator.tick() == []  # No catch-up sound burst.


def test_ack_cancels_queued_first_and_survives_updates_but_not_new_ids():
    coordinator, clock, _ = setup()
    coordinator.ingest([event()])
    assert coordinator.acknowledge(wall_time=123) == ["a"]
    coordinator.ingest([event(hit_count=20, reminder=True)])
    assert coordinator.acknowledged_at == {"a": 123}
    clock.now = 20
    assert coordinator.tick() == []
    coordinator.ingest([event("b")])
    clock.now += 0.3
    first, = coordinator.tick()
    assert [e.id for e in first.events] == ["b"]
    assert set(coordinator.active) == {"a", "b"}


def test_context_revision_edit_and_end_cancel_pending_and_cannot_reopen():
    coordinator, clock, region = setup()
    coordinator.ingest([event()])
    coordinator.set_context("session", True, [region], editing_ids=["r"])
    coordinator.set_context("session", True, [region])
    assert coordinator.ingest([event()]) == []
    coordinator.ingest([event("b")])
    region.revision = 1
    coordinator.set_context("session", True, [region])
    assert not coordinator.active
    assert coordinator.ingest([event("c")]) == []
    coordinator.ingest([event("d", revision=1)])
    coordinator.ingest([event("d", revision=1, ended_at=150, end_reason="recovered")])
    assert coordinator.ingest([event("d", revision=1)]) == []
    clock.now = 30
    assert coordinator.tick() == []


def test_stream_without_regions_old_run_ends_archive_and_shutdown():
    coordinator, clock, _ = setup()
    coordinator.set_context("session", True, [])
    stream = event(anomaly_type="stream", region_id="")
    coordinator.ingest([stream])
    clock.now = 0.3
    assert coordinator.tick()[0].events[0].anomaly_type == "stream"
    coordinator.set_context("new-session", True, [])
    assert coordinator.ingest([stream]) == []
    accepted = coordinator.ingest([replace(stream, ended_at=200)])
    assert accepted[0].phase == "end"
    clock.now = 10
    assert coordinator.tick() == []
    coordinator.shutdown()
    assert coordinator.ingest([event(anomaly_type="test")]) == []
    assert coordinator.tick() == []


def test_run_generation_stale_stream_is_rejected():
    coordinator, _, _ = setup()
    coordinator.set_context("session", True, [], run_id=2)
    stream = event(anomaly_type="stream", region_id="")
    assert coordinator.ingest([stream]) == []
    assert coordinator.ingest([replace(stream, run_id=2)])[0].phase == "start"


def test_tests_and_interruptions_never_become_active_and_closed_ids_bounded():
    clock = Clock()
    coordinator = AlarmCoordinator(clock=clock, closed_limit=2)
    coordinator.set_context("session", True, [])
    interruption = event(anomaly_type="interruption", ended_at=100)
    assert coordinator.ingest([interruption])[0].phase == "interruption"
    assert coordinator.tick() == []
    test = event("test", anomaly_type="test")
    assert coordinator.ingest([test])[0].phase == "test"
    assert coordinator.ingest([test]) == []
    clock.now = 0.3
    assert coordinator.tick()[0].phase == "first"
    assert not coordinator.active
    clock.now = 20
    assert coordinator.tick() == []
    for index in range(10):
        coordinator.ingest([event(str(index), ended_at=200)])
    assert len(coordinator.closed) == 2


def test_suspend_cancels_first_preserves_activity_and_resumes_as_delayed_repeat():
    coordinator, clock, _ = setup()
    coordinator.ingest([event(), event("ack")])
    coordinator.acknowledge(wall_time=123)
    coordinator.ingest([event("pending")])
    coordinator.set_suspended(True)
    clock.now = 10
    coordinator.ingest([event("pending", hit_count=8), event("during")])
    assert coordinator.tick() == []
    assert set(coordinator.active) == {"a", "ack", "pending", "during"}
    assert coordinator.acknowledged_at == {"a": 123, "ack": 123}
    coordinator.set_suspended(False)
    assert coordinator.tick() == []
    clock.now = 14.99
    assert coordinator.tick() == []
    coordinator.set_suspended(False)  # Idempotent; does not defer the due time.
    clock.now = 15
    repeat, = coordinator.tick()
    assert repeat.phase == "repeat"
    assert {e.id for e in repeat.events} == {"pending", "during"}
    assert coordinator.active["pending"].hit_count == 8


def test_suspend_discards_test_demonstrations_and_periodic_backlog():
    coordinator, clock, _ = setup()
    coordinator.ingest([event()])
    clock.now = 0.3
    assert coordinator.tick()[0].phase == "first"
    coordinator.ingest([event("before", anomaly_type="test")])
    coordinator.set_suspended(True)
    coordinator.ingest([event("during", anomaly_type="test")])
    clock.now = 100
    assert coordinator.tick() == []
    coordinator.set_suspended(False)
    assert coordinator.tick() == []
    clock.now = 105
    repeat, = coordinator.tick()
    assert repeat.phase == "repeat" and [e.id for e in repeat.events] == ["a"]


def test_ends_and_acknowledgments_during_suspend_do_not_resume_reminders():
    coordinator, clock, _ = setup()
    coordinator.ingest([event(), event("ended")])
    coordinator.set_suspended(True)
    coordinator.ingest([event("ended", ended_at=200)])
    assert coordinator.acknowledge(wall_time=201) == ["a"]
    coordinator.set_suspended(False)
    clock.now = 100
    assert coordinator.tick() == []
