"""Focused worker contracts; no VLC, network, or real media required."""
from dataclasses import replace
import threading
import time

import numpy as np
import pytest

from vlc_monitor.models import AppConfig, RegionConfig, FrameSample
from vlc_monitor.monitoring import controller
from vlc_monitor.playback.frame_buffer import LatestFrame


class Backend:
    instances = []

    def __init__(self, location):
        self.session_id = 's'
        self.metadata_error = ''
        self.preview_frames = LatestFrame()
        self.frame = None
        self.state = 'Opening'
        self.closed = False
        self.instances.append(self)

    def play(self, source):
        pass

    def poll_frame(self):
        return self.frame

    def status(self):
        return self.state

    def refresh_metadata(self):
        pass

    def close(self):
        self.closed = True


@pytest.fixture
def worker(monkeypatch):
    Backend.instances.clear()
    monkeypatch.setattr(controller, 'VlcBackend', Backend)
    region = RegionConfig(id='r', rect=(0, 0, 1, 1))
    return controller.MonitorWorker(None, AppConfig(regions=[region]))


def fresh_frame():
    return FrameSample('s', 1, time.monotonic(), np.full((64, 64, 4), 100, dtype=np.uint8), 1)


def command(worker, action, rid, **payload):
    worker.execute(dict(action=action, request_id=rid, **payload))


def test_connect_is_waiting_until_frame_and_reconnect_never_auto_monitors(worker):
    statuses, results = [], []
    worker.status.connect(statuses.append)
    worker.command_result.connect(results.append)
    command(worker, 'connect', '1', source='fake')
    assert worker.playback_state == 'waiting' and not results
    assert not worker.can_monitor()[0]
    worker.backend.frame = fresh_frame()
    worker.backend.state = 'Playing'
    worker.poll()
    assert results[-1]['success'] and worker.can_monitor()[0]
    command(worker, 'start', '2')
    assert worker.engine.running and worker.run_id == 1
    command(worker, 'connect', '3', source='other')
    assert not worker.engine.running and Backend.instances[0].closed
    worker.backend.frame = fresh_frame()
    worker.poll()
    assert worker.playback_state == 'playing' and not worker.engine.running
    assert all(b['state_version'] > a['state_version'] for a, b in zip(statuses, statuses[1:]))


def test_cancel_waiting_releases_player_and_late_frame_cannot_complete(worker):
    results = []
    worker.command_result.connect(results.append)
    cancel = threading.Event()
    command(worker, 'connect', 'open', source='fake', cancel=cancel)
    old = worker.backend
    cancel.set()
    old.frame = fresh_frame()
    worker.poll()
    command(worker, 'cancel', 'cancel', target='open')
    assert old.closed and worker.backend is None
    assert not next(r for r in results if r['request_id'] == 'open')['success']
    assert results[-1]['request_id'] == 'cancel' and results[-1]['success']
    assert len(Backend.instances) == 1


def test_cancel_during_synchronous_play_has_safe_cleanup(worker, monkeypatch):
    cancel = threading.Event()
    monkeypatch.setattr(Backend, 'play', lambda *args: cancel.set())
    command(worker, 'connect', 'open', source='fake', cancel=cancel)
    assert Backend.instances[0].closed and worker.backend is None
    assert worker.playback_state == 'disconnected'


def test_media_error_does_not_erase_monitoring_or_stream_alarms(worker):
    command(worker, 'connect', 'open', source='fake')
    worker.backend.frame = fresh_frame()
    worker.poll()
    command(worker, 'start', 'start')
    worker.backend.state = 'Error'
    worker.backend.frame = None
    worker.poll()
    assert worker.playback_state == 'error' and worker.engine.running
    assert not worker.can_monitor()[0]
    command(worker, 'stop', 'stop')
    assert not worker.engine.running


def test_can_monitor_shared_validation_and_pending_failure_release(worker):
    results = []
    worker.command_result.connect(results.append)
    command(worker, 'start', 'no-frame')
    assert not results[-1]['success']
    command(worker, 'connect', 'open', source='fake')
    worker.backend.frame = fresh_frame()
    worker.poll()
    worker.paused_revisions['r'] = 0
    assert not worker.can_monitor()[0]
    worker.paused_revisions.clear()
    worker.regions = [replace(worker.regions[0], rect=(0, 0, .1, .1))]
    assert not worker.can_monitor()[0]


def test_new_run_status_is_published_before_its_events(worker):
    ordered = []
    worker.status.connect(lambda state: ordered.append(('status', state)))
    worker.events.connect(lambda events: ordered.append(('events', events)))
    command(worker, 'connect', 'open', source='fake')
    worker.backend.frame = fresh_frame()
    worker.poll()
    command(worker, 'start', 'start')
    events = worker.engine.tick(time.monotonic() + 6)
    assert events
    worker.emit_events(events)
    assert ordered[-2][0] == 'status' and ordered[-1][0] == 'events'
    assert ordered[-2][1]['run_id'] == ordered[-1][1][0].run_id == 1


def test_cancel_after_worker_success_before_gui_receives_reply(worker):
    command(worker, 'connect', 'open', source='fake', cancel=threading.Event())
    backend = worker.backend
    backend.frame = fresh_frame()
    worker.poll()
    assert worker.open_request is None and worker.playback_state == 'playing'
    command(worker, 'cancel', 'cancel', target='open')
    assert backend.closed and worker.backend is None
    assert worker.playback_state == 'disconnected'


def test_cleanup_failure_reports_and_retains_owner_until_retry(worker, monkeypatch):
    results, shutdown_errors, finished = [], [], []
    worker.command_result.connect(results.append)
    worker.shutdown_failed.connect(shutdown_errors.append)
    worker.finished.connect(lambda: finished.append(True))
    command(worker, 'connect', 'open', source='fake')
    backend = worker.backend
    backend.frame = fresh_frame()
    worker.poll()
    def fail_close():
        raise RuntimeError('fake cleanup error')
    monkeypatch.setattr(backend, 'close', fail_close)
    command(worker, 'disconnect', 'stop')
    assert results[-1]['request_id'] == 'stop' and not results[-1]['success']
    assert worker.backend is backend and worker.cleanup_failed
    command(worker, 'connect', 'new', source='other')
    assert not results[-1]['success'] and len(Backend.instances) == 1
    worker.shutdown()
    assert shutdown_errors and not finished and worker.backend is backend
    monkeypatch.setattr(backend, 'close', lambda: setattr(backend, 'closed', True))
    command(worker, 'disconnect', 'retry')
    assert results[-1]['success'] and worker.backend is None
    worker.shutdown()
    assert finished


def test_poll_timeout_cleanup_failure_still_completes_open(worker, monkeypatch):
    results = []
    worker.command_result.connect(results.append)
    command(worker, 'connect', 'open', source='fake')
    worker.opened_at = time.monotonic() - 60
    def fail_close():
        raise RuntimeError('fake cleanup failure')
    monkeypatch.setattr(worker.backend, 'close', fail_close)
    worker.poll()
    assert worker.cleanup_failed and worker.playback_state == 'cleanup_error'
    assert results[-1]['request_id'] == 'open' and not results[-1]['success']


@pytest.mark.parametrize('future', [False, True])
def test_poll_uses_post_snapshot_clock_without_accepting_future_frame(worker, monkeypatch, future):
    clock = [100.0]
    monkeypatch.setattr(controller.time, 'monotonic', lambda: clock[0])
    command(worker, 'connect', 'open', source='fake')
    worker.backend.state = 'Playing'
    worker.engine.start('s', worker.regions, clock[0])
    seen = []
    worker.events.connect(seen.extend)
    for seq in range(1, worker.regions[0].black_count + 1):
        clock[0] = 100.0 + seq
        def snapshot():
            clock[0] += .001
            return FrameSample('s', seq, clock[0] + (1 if future else 0),
                               np.zeros((64, 64, 4), dtype=np.uint8), 1)
        monkeypatch.setattr(worker.backend, 'poll_frame', snapshot)
        worker.poll()
    black = [event for event in seen if event.anomaly_type == 'black']
    assert bool(black) is not future
    if not future:
        assert black[-1].hit_count == worker.regions[0].black_count
