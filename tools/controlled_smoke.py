"""Bounded native playback/UI/storage smoke with generated media and no accounts."""
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication

from vlc_monitor.models import AppConfig, RegionConfig
from vlc_monitor.notifications.wechat_service import WechatNotificationService
from vlc_monitor.platform.vlc_locator import locate
from vlc_monitor.storage.event_store import EventStore
from vlc_monitor.ui.main_window import MainWindow


def main():
    root = Path(__file__).resolve().parents[1]
    output = root / "tmp/controlled-smoke"
    output.mkdir(parents=True, exist_ok=True)
    media = output / "normal-black-recovery.y4m"
    w, h, fps = 96, 64, 10
    with media.open("wb") as stream:
        stream.write(f"YUV4MPEG2 W{w} H{h} F{fps}:1 Ip A1:1 C420jpeg\n".encode())
        for index in range(220):
            value = 16 if 30 <= index < 80 else 110 + index % 70
            stream.write(b"FRAME\n" + bytes([value]) * (w * h) + bytes([128]) * (w * h // 2))
    app = QApplication.instance() or QApplication([])
    app.setQuitOnLastWindowClosed(False)
    accesses = []

    class Store:
        def load(self, *_):
            accesses.append("credential")
            raise RuntimeError("Smoke cannot read credentials")

    class Transport:
        def request(self, *_args, **_kwargs):
            accesses.append("network")
            raise RuntimeError("Smoke cannot request the network")

        def cancel(self):
            pass

    def service(*args, **kwargs):
        return WechatNotificationService(*args, **kwargs, factory=lambda _: (Store(), Transport()))

    def pump(predicate, timeout=4):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            app.processEvents()
            if predicate():
                return
            time.sleep(.005)
        raise AssertionError("Controlled smoke deadline exceeded")

    with tempfile.TemporaryDirectory(prefix="vlc-controlled-") as directory:
        config = AppConfig(source=str(media), detection_interval=.2, sound_enabled=False,
                           foreground_on_alarm=False, wechat_enabled=False,
                           regions=[RegionConfig(id="synthetic", name="合成画面", rect=(0, 0, 1, 1),
                                                 black_count=2, recovery_count=2, freeze_enabled=False)])
        window = MainWindow(locate(), config, directory, wechat_factory=service)
        heartbeat_times = []
        heartbeat = QTimer()
        heartbeat.timeout.connect(lambda: heartbeat_times.append(time.monotonic()))
        heartbeat.start(10)
        window.show()
        report = {"scope": "Generated local media; isolated data; fake credentials/transport; NOT live acceptance"}
        lock = None
        try:
            window.connect_source()
            pump(lambda: window.status_data.get("can_monitor") and not window.pending, 10)
            window.start_monitoring()
            pump(lambda: window.status_data.get("running") and not window.pending)
            lock = sqlite3.connect(Path(directory) / "history.sqlite3", timeout=.1)
            lock.execute("BEGIN IMMEDIATE")
            before = len(heartbeat_times)
            pump(lambda: bool(window.attention.active), 10)
            pump(lambda: window._storage_failed, 2)
            report["gui_heartbeats_while_database_locked"] = len(heartbeat_times) - before
            assert report["gui_heartbeats_while_database_locked"] > 20
            assert not window.event_store.closing
            lock.rollback()
            lock.close()
            lock = None
            pump(lambda: any(row.get("end_reason") == "恢复正常" for row in window.history.values()), 10)
            report["observed"] = ["native fresh frames", "black alarm", "visible memory-only storage warning",
                                  "normal recovery", "GUI responsive during database lock"]
            window.submit_command("stop")
            pump(lambda: not window.pending and not window.status_data.get("running"))
            window.stop_playback()
            pump(lambda: not window.pending and window.status_data.get("playback_state") == "disconnected")
            window.connect_source()
            pump(lambda: not window.pending and window.status_data.get("can_monitor"), 10)
            assert not window.status_data.get("running")
            report["observed"].append("stop/reconnect does not auto-monitor")
        finally:
            if lock is not None:
                lock.rollback()
                lock.close()
            window.dirty = False
            window.status_data["running"] = False
            window.close()
            pump(lambda: window.event_store.is_closed, 8)
            heartbeat.stop()
        store = EventStore(Path(directory) / "history.sqlite3")
        rows = store.recent()
        store.close()
        assert any(row["end_reason"] == "恢复正常" for row in rows)
        assert not accesses, "Smoke attempted external credential/network access"
        report.update(history_rows=len(rows), credential_reads=0, wechat_network_requests=0,
                      safe_close=not window.thread.isRunning() and not window.wechat.thread.isRunning(),
                      largest_gui_heartbeat_gap_ms=round(max(b - a for a, b in zip(heartbeat_times, heartbeat_times[1:])) * 1000, 1))
        (output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
