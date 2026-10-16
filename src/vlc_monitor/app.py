import argparse
import json
from pathlib import Path
import platform
import sys
import time

from vlc_monitor import __version__
from vlc_monitor.platform.vlc_locator import locate, VlcError


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--diagnose", action="store_true")
    parser.add_argument("--vlc-path", default="")
    parser.add_argument("--probe", metavar="LOCAL_VIDEO")
    parser.add_argument("--duration", type=float, default=30)
    parser.add_argument("--report", default="tmp/probe.json")
    args = parser.parse_args()
    if args.diagnose:
        try:
            from vlc_monitor.platform.vlc_locator import load_binding
            location = locate(args.vlc_path)
            vlc = load_binding(location)
            print(json.dumps({"application": __version__, "system": platform.platform(), "python": platform.python_version(), "vlc": vlc.libvlc_get_version().decode(), "library": str(location.library), "plugins": str(location.plugins)}, ensure_ascii=False, indent=2))
            return 0
        except VlcError as exc:
            print(json.dumps({"error": exc.code, "message": str(exc)}, ensure_ascii=False))
            return 1
    from PySide6.QtWidgets import QApplication, QMessageBox
    from vlc_monitor.ui.vlc_picker import choose_vlc_path
    app = QApplication(sys.argv[:1])
    app.setOrganizationName("LocalTools")
    app.setApplicationName("VLCMonitor")
    app.setApplicationVersion(__version__)
    from PySide6.QtCore import QStandardPaths
    from vlc_monitor.storage.config_store import ConfigStore, ConfigError
    data_dir = Path(QStandardPaths.writableLocation(QStandardPaths.StandardLocation.AppLocalDataLocation))
    store = ConfigStore(data_dir / "config.json")
    config_problem = False
    try:
        config = store.load()
    except ConfigError as exc:
        from vlc_monitor.models import AppConfig
        config = AppConfig()
        config_problem = True
        QMessageBox.warning(None, "配置异常", str(exc))
    selected = args.vlc_path or config.vlc_path
    while True:
        try:
            location = locate(selected)
            from vlc_monitor.platform.vlc_locator import load_binding
            load_binding(location)
            break
        except VlcError as exc:
            box = QMessageBox(QMessageBox.Icon.Warning, "需要 VLC", f"{exc}\n请选择 VLC 位置后继续。")
            browse = box.addButton("选择 VLC 位置", QMessageBox.ButtonRole.AcceptRole)
            quit_button = box.addButton("退出", QMessageBox.ButtonRole.RejectRole)
            box.setDefaultButton(quit_button)
            box.setEscapeButton(quit_button)
            box.exec()
            if box.clickedButton() is not browse:
                return 1
            selected = choose_vlc_path(None, selected, startup=True)
            if not selected:
                return 1
    if args.probe:
        return run_probe(app, location, args)
    from vlc_monitor.ui.main_window import MainWindow
    config.vlc_path = str(location.root)
    window = MainWindow(location, config, data_dir, config_problem)
    window.show()
    return app.exec()


def run_probe(app, location, args):
    import psutil
    from PySide6.QtCore import QTimer
    from PySide6.QtGui import QImage, QPixmap
    from PySide6.QtWidgets import QLabel
    from vlc_monitor.playback.vlc_backend import VlcBackend
    backend = VlcBackend(location)
    backend.play(str(Path(args.probe).resolve()))
    label = QLabel("等待视频")
    label.setWindowTitle("VLC 单路取帧技术验证")
    label.resize(800, 500)
    label.show()
    process = psutil.Process()
    process.cpu_percent()
    start = time.monotonic()
    rows = []
    previous = 0
    def preview():
        backend.poll_frame()
        frame = backend.preview_frames.latest()
        if frame and not label.isMinimized():
            image = QImage(frame.pixels.data, frame.width, frame.height, frame.stride, QImage.Format.Format_RGB32)
            label.setPixmap(QPixmap.fromImage(image).scaled(label.size()))

    def sample():
        nonlocal previous
        backend.refresh_metadata()
        frame = backend.poll_frame()
        elapsed = time.monotonic() - start
        if frame:
            rows.append({"elapsed": round(elapsed, 3), "seq": frame.frame_seq, "decoded_count": frame.decoded_count, "new": frame.frame_seq != previous, "minimized": label.isMinimized(), "rss_mb": round(process.memory_info().rss / 1024**2, 2), "cpu_percent": process.cpu_percent(), "age": round(time.monotonic() - frame.received_at, 3)})
            previous = frame.frame_seq
        if elapsed >= args.duration:
            label.close()
    timer = QTimer()
    timer.timeout.connect(sample)
    timer.start(1000)
    preview_timer = QTimer()
    preview_timer.timeout.connect(preview)
    preview_timer.start(50)
    QTimer.singleShot(5000, label.showMinimized)
    QTimer.singleShot(int(max(6000, args.duration * 700)), label.showNormal)
    app.exec()
    timer.stop()
    preview_timer.stop()
    backend.close()
    path = Path(args.report)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"source": "local synthetic 640x360 MJPEG 25fps; not live acceptance", "single_player": True, "duration": time.monotonic() - start, "rows": rows}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
