"""Only the UI integration boundaries not covered by pure policy tests."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
import time

import pytest
from PySide6.QtWidgets import QApplication, QMenu, QPushButton
from vlc_monitor.models import AlarmEvent, AppConfig, RegionConfig
from vlc_monitor.ui.main_window import MainWindow
from vlc_monitor.platform.vlc_locator import VlcLocation
from pathlib import Path


@pytest.fixture
def window(tmp_path):
    app = QApplication.instance() or QApplication([])
    location = VlcLocation(Path('/unloaded'), Path('/unloaded/library'), Path('/unloaded/plugins'))
    window = MainWindow(location, AppConfig(regions=[RegionConfig(id='r')]), tmp_path)
    yield window
    for menu in window.findChildren(QMenu):
        menu.hide()
    window.dirty = False
    window.status_data['running'] = False
    window.close()
    deadline = time.monotonic() + 3
    while (window.thread.isRunning() or window.wechat.thread.isRunning() or not window.event_store.is_closed) and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(.005)
    assert not window.thread.isRunning()
    assert not window.wechat.thread.isRunning()
    assert window.event_store.is_closed


def test_compact_entries_and_minimum_size_do_not_hide_rule_controls(window, monkeypatch):
    window.resize(920, 640)
    window.select_region('r')
    window.show()
    QApplication.processEvents()
    assert window.black_count.height() >= 24
    assert not window.findChildren(QMenu)
    assert window.settings_button.text() == '设置'
    assert window.settings_button.isFlat() and window.save_button.isFlat()
    assert window.mute_button.isFlat() and window.add_region_button.isFlat()
    assert window.file_action in window.source.actions()
    assert not any(button.text() == '本地视频' for button in window.findChildren(QPushButton))
    calls = []
    monkeypatch.setattr(window, 'connect_source', lambda: calls.append('connect'))
    window.status_data.update(playback_state='playing', running=False)
    window.source.setText('new-source')
    window.play_from_enter()
    assert not calls
    window.status_data['playback_state'] = 'disconnected'
    window.play_from_enter()
    assert calls == ['connect']
    window.canvas.set_drawing(True)
    from PySide6.QtTest import QTest
    from PySide6.QtCore import Qt
    QTest.keyClick(window.canvas, Qt.Key.Key_Escape)
    assert not window.canvas._drawing


def test_old_connect_reply_cannot_release_cancel_pending(window):
    window.pending = {'request_id': 'cancel-new', 'action': 'cancel', 'target': 'open-old'}
    window.on_command_result({'request_id': 'open-old', 'action': 'connect', 'success': True})
    assert window.pending['request_id'] == 'cancel-new'
    window.on_command_result({'request_id': 'cancel-new', 'action': 'cancel', 'success': True})
    assert window.pending is None


def test_stream_without_preview_and_ack_survives_later_snapshots(window, monkeypatch):
    import vlc_monitor.ui.main_window as ui
    from dataclasses import replace
    called = []
    monkeypatch.setattr(ui, 'request_attention', lambda *args, **kwargs: called.append(kwargs))
    window.apply_local_settings(dict(sound_enabled=False, foreground_on_alarm=True, local_alert_interval_seconds=5))
    window.on_status(dict(state_version=20, session='s', run_id=2, running=True, playback_state='error', can_monitor=False, statuses={}, stream='告警', problem='', age=None, seq=0))
    now = time.time()
    event = AlarmEvent('stream', 's', '', '视频流', 'stream', 0, now, now, now, 1, run_id=2)
    window.attention.clock = lambda: 0
    window.on_events([event])
    assert event.id in window.attention.active and window.worker.frames.latest() is None
    window.attention.clock = lambda: 1
    window.dispatch_attention()
    assert len(called) == 1 and called[0]['foreground']
    window.acknowledge_alarms()
    window.on_events([replace(event, hit_count=5)])
    assert window.history[event.id]['acknowledged_at'] is not None
    loaded = []
    window.event_store.loaded.connect(loaded.extend)
    assert window.event_store.recent()
    deadline = time.monotonic() + 2
    while not loaded and time.monotonic() < deadline:
        QApplication.processEvents()
        time.sleep(.005)
    assert loaded[0]['acknowledged_at'] is not None
    window.on_status(dict(state_version=19))
    assert window.status_data['state_version'] == 20


def test_startup_history_does_not_replace_newer_terminal_receipt(window):
    window.history['late'] = {'id': 'late', 'triggered_at': 2, 'ended_at': 3,
                              'acknowledged_at': 2.5, 'delivery': {'result': 'accepted'}}
    window.refresh_history = lambda: None
    window.on_history_loaded([{'id': 'late', 'triggered_at': 1, 'ended_at': None,
                               'acknowledged_at': None, 'delivery': {'result': 'failed'}}])
    assert window.history['late']['ended_at'] == 3
    assert window.history['late']['acknowledged_at'] == 2.5
    assert window.history['late']['delivery']['result'] == 'accepted'


def test_shutdown_waits_for_final_receipt_and_storage_barrier(window):
    from dataclasses import asdict
    from PySide6.QtCore import QObject, Qt, Signal
    from vlc_monitor.storage.event_store import EventStore

    class FinalCallbacks(QObject):
        events = Signal(object)
        delivery = Signal(object)

    final = FinalCallbacks()
    final.events.connect(window.on_events, Qt.ConnectionType.QueuedConnection)
    final.delivery.connect(window.on_wechat_delivery, Qt.ConnectionType.QueuedConnection)
    now = time.time()
    event = AlarmEvent('final', 's', 'r', '结束记录', 'black', 0, now, now, now, 4,
                       ended_at=now + 1, end_reason='手动停止')
    window.history[event.id] = asdict(event)
    window.dirty = False
    window.close()
    final.events.emit([event])
    final.delivery.emit({'event_ids': [event.id], 'phase': 'start',
                         'result': 'cancelled', 'sequence': 0, 'attempts': 1})
    deadline = time.monotonic() + 3
    while not window.event_store.is_closed and time.monotonic() < deadline:
        QApplication.processEvents()
        time.sleep(.005)
    assert window.event_store.is_closed
    store = EventStore(window.data_dir / 'history.sqlite3')
    row = store.recent()[0]
    store.close()
    assert row['end_reason'] == '手动停止'
    assert row['delivery']['result'] == 'cancelled'


def test_failed_player_shutdown_leaves_storage_reusable(window, monkeypatch):
    from PySide6.QtWidgets import QMessageBox
    monkeypatch.setattr(QMessageBox, 'warning', lambda *_: None)
    window._closing = True
    window.on_shutdown_failed('受控停止失败')
    assert not window._closing and not window.event_store.closing
    assert not window._storage_close_started
    assert window.event_store.acknowledge(['still-open'], time.time())
