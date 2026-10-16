"""Render an isolated Qt design preview: synthetic pixels, no app services.

Run from the project root with PYTHONPATH=src .venv/bin/python tools/ui_preview.py.
This is a layout prototype, not an application integration or acceptance test.
"""
import os
from pathlib import Path
import platform
import sys
import tempfile
import time
from datetime import datetime

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np
from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtGui import QColor, QFont, QImage, QPainter, QLinearGradient
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QFormLayout, QHBoxLayout, QHeaderView, QLabel,
    QGroupBox, QLayout, QLineEdit, QListWidget, QMainWindow, QPushButton,
    QScrollArea, QSpinBox, QSplitter, QStyle, QTableWidget,
    QTableWidgetItem, QVBoxLayout, QWidget,
)

from vlc_monitor.models import AppConfig, RegionConfig
from vlc_monitor.playback.frame_buffer import FrameSample
from vlc_monitor.ui.video_canvas import VideoCanvas


STYLE = """
QWidget { color:#243447; background:#f1f3f5; }
QLabel#brand { font-size:23px; font-weight:700; color:#152b40; }
QLabel#muted { color:#5e7388; }
QLabel#section { font-size:15px; font-weight:600; color:#152b40; }
QPushButton { background:#fff; border:1px solid #c6d0da; border-radius:5px; padding:7px 13px; }
QPushButton#primary { background:#bd4b29; color:white; border-color:#bd4b29; font-weight:600; }
QPushButton:disabled { color:#9da8b3; background:#e3e8ec; border-color:#d5dde3; }
QLineEdit,QSpinBox,QListWidget,QTableWidget { background:#fff; border:1px solid #ccd5dd; border-radius:4px; padding:5px; }
QListWidget::item { padding:9px 5px; border-bottom:1px solid #edf0f3; }
QListWidget::item:selected { background:#dce9ef; color:#17374b; }
QHeaderView::section { background:#e3e9ed; padding:6px; border:none; font-weight:600; }
QTableWidget { gridline-color:#e4e9ed; }
QGroupBox { border:1px solid #d1dbe2; border-radius:6px; margin-top:14px; padding:12px 9px 8px; font-weight:600; }
QGroupBox::title { subcontrol-origin:margin; left:9px; padding:0 4px; }
QTabWidget::pane { border:1px solid #ccd5dd; }
QTabBar::tab { padding:10px 18px; }
"""


def button(text, primary=False):
    value = QPushButton(text)
    if primary:
        value.setObjectName("primary")
    return value


def label(text, name=""):
    value = QLabel(text)
    value.setObjectName(name)
    return value


def synthetic_frame(alarm):
    image = QImage(1280, 720, QImage.Format.Format_RGB32)
    painter = QPainter(image)
    gradient = QLinearGradient(0, 0, 1280, 720)
    gradient.setColorAt(0, QColor("#24445b"))
    gradient.setColorAt(1, QColor("#102635"))
    painter.fillRect(image.rect(), gradient)
    painter.setPen(QColor("#426075"))
    for x in range(0, 1280, 80):
        painter.drawLine(x, 0, x, 720)
    for y in range(0, 720, 80):
        painter.drawLine(0, y, 1280, y)
    for index, color in enumerate(("#9bb6c4", "#b8c4ac", "#c8b697", "#af9993", "#9f9fb4", "#819eab")):
        painter.fillRect(120 + index * 170, 220, 170, 225, QColor(color))
    if alarm:
        painter.fillRect(120, 220, 510, 225, QColor("#080b10"))
    painter.setPen(QColor("#d8e4eb"))
    painter.setFont(QFont("PingFang SC", 25))
    painter.drawText(55, 85, "演示画面 · 合成视频")
    painter.setFont(QFont("PingFang SC", 18))
    painter.drawText(55, 650, "仅用于界面预览，不连接视频源或微信")
    painter.end()
    pixels = np.frombuffer(image.bits(), dtype=np.uint8).reshape(720, 1280, 4).copy()
    pixels.flags.writeable = False
    return FrameSample("isolated-preview", 1, 0, pixels, 16 / 9)


def main_preview(alarm=False):
    window = QMainWindow()
    window.setWindowTitle("VLC 监播 · 界面演示")
    window.setMinimumSize(920, 640)
    center = QWidget()
    window.setCentralWidget(center)
    layout = QVBoxLayout(center)
    layout.setContentsMargins(20, 16, 20, 12)
    layout.setSpacing(10)
    heading = QHBoxLayout()
    heading.addWidget(label("VLC 监播", "brand"))
    heading.addWidget(label("界面演示", "muted"))
    heading.addStretch()
    heading.addWidget(button("设置"))
    heading.addWidget(button("保存配置"))
    layout.addLayout(heading)
    source = QHBoxLayout()
    source.addWidget(label("视频来源"))
    source.addWidget(QLineEdit("演示视频（合成画面）"), 1)
    source.addWidget(button("本地视频"))
    source.addWidget(button("停止播放"))
    layout.addLayout(source)
    actions = QHBoxLayout()
    actions.addWidget(button("停止监播", True))
    actions.addStretch()
    actions.addWidget(button("静音 5 分钟"))
    layout.addLayout(actions)
    workspace = QHBoxLayout()
    workspace.setSpacing(16)
    canvas = VideoCanvas()
    canvas.set_frame(synthetic_frame(alarm))
    canvas.set_regions([
        RegionConfig(id="main", name="主画面", rect=(.09, .30, .40, .33)),
        RegionConfig(id="backup", name="辅助画面", rect=(.51, .30, .38, .33)),
    ])
    canvas.set_statuses({"main": {"black": "告警" if alarm else "正常"}, "backup": "正常"})
    canvas.set_selected("main")
    workspace.addWidget(canvas, 1)
    panel = QWidget()
    panel.setMinimumHeight(375)
    side = QVBoxLayout(panel)
    side.setContentsMargins(0, 0, 0, 0)
    title = QHBoxLayout()
    title.addWidget(label("监播区域", "section"))
    title.addStretch()
    title.addWidget(button("新增区域"))
    side.addLayout(title)
    regions = QListWidget()
    regions.addItems(["主画面", "辅助画面"])
    regions.setCurrentRow(0)
    regions.setMaximumHeight(105)
    side.addWidget(regions)
    form = QFormLayout()
    form.setSpacing(9)
    form.addRow("名称", QLineEdit("主画面"))
    enabled = QCheckBox("启用此区域")
    enabled.setChecked(True)
    form.addRow(enabled)
    checks = QHBoxLayout()
    for text in ("黑屏", "疑似静帧"):
        check = QCheckBox(text)
        check.setChecked(True)
        checks.addWidget(check)
    form.addRow("检测类型", checks)
    for text, value in (("黑屏连续次数", 4), ("静帧连续次数", 10), ("恢复连续次数", 3)):
        spin = QSpinBox()
        spin.setRange(1, 3600)
        spin.setValue(value)
        spin.setSuffix(" 次")
        form.addRow(text, spin)
    side.addLayout(form)
    edit = QHBoxLayout()
    for text in ("应用", "取消"):
        control = button(text)
        control.setEnabled(False)
        edit.addWidget(control)
    edit.addStretch()
    edit.addWidget(button("删除"))
    side.addLayout(edit)
    side.addStretch()
    scroll = QScrollArea()
    scroll.setWidgetResizable(True)
    scroll.setFixedWidth(286)
    scroll.setFrameShape(QScrollArea.Shape.NoFrame)
    scroll.setWidget(panel)
    workspace.addWidget(scroll)
    layout.addLayout(workspace, 1)
    layout.addWidget(label("在画面中拖动或缩放区域；修改规则后点击应用。", "muted"))
    if alarm:
        strip = QWidget()
        strip.setStyleSheet("QWidget {background:#fae6df; border-radius:5px;} QLabel {color:#8d301f;}")
        row = QHBoxLayout(strip)
        row.setContentsMargins(12, 6, 8, 6)
        row.addWidget(label("1 项异常，1 项未确认 · 主画面黑屏"), 1)
        row.addWidget(button("确认告警"))
        layout.addWidget(strip)
    layout.addWidget(label("告警与恢复", "section"))
    table = QTableWidget(2 if alarm else 1, 6)
    table.setHorizontalHeaderLabels(["时间", "区域", "类型", "命中/经过", "状态", "通知"])
    table.verticalHeader().hide()
    table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
    table.setFixedHeight(113)
    rows = [["10:24:18", "辅助画面", "疑似静帧", "10 次 / 10 秒", "恢复正常", "接口接受"]]
    if alarm:
        rows.insert(0, ["10:26:04", "主画面", "黑屏", "4 次 / 4 秒", "异常 · 未确认", "接口接受"])
    for index, values in enumerate(rows):
        for column, text in enumerate(values):
            table.setItem(index, column, QTableWidgetItem(text))
    layout.addWidget(table)
    layout.addWidget(label("播放中 · 监播中 · 声音开启 · 微信通知已开启（演示）", "muted"))
    window.resize(1240, 820)
    return window


class FakeWechatService(QObject):
    """Only in-memory snapshots; never imports notification or credential code."""
    updated = Signal(object)

    def __init__(self):
        super().__init__()
        self.snapshot = dict(state="trial_sendable", busy=False, enabled=True,
                             result="test_api_accepted", pending_count=0)

    def command(self, *_):
        raise RuntimeError("Preview actions do not call application services")


class ActualPreviewWechat(FakeWechatService):
    delivery = Signal(object)
    settingsChanged = Signal(object)
    stopped = Signal()

    def __init__(self, _data_dir, *, parent=None, **_options):
        super().__init__()
        self.setParent(parent)
        from PySide6.QtCore import QThread
        self.thread = QThread(self)
        self.snapshot["real_alarms_available"] = True

    def set_context(self, *_args, **_kwargs):
        pass

    def publish(self, _events):
        pass

    def quiesce(self):
        pass

    def shutdown(self):
        self.stopped.emit()


def render_actual(app):
    """Exercise the actual UI with temporary persistence and inert notifications."""
    from vlc_monitor.models import AlarmEvent
    from vlc_monitor.ui.main_window import MainWindow
    from vlc_monitor.ui.settings_dialog import SettingsDialog

    root = Path(__file__).resolve().parents[1]
    output = root / "tmp" / "ui-actual"
    output.mkdir(parents=True, exist_ok=True)
    app.setStyleSheet("")
    app.setQuitOnLastWindowClosed(False)

    def pump(seconds=.08):
        until = time.monotonic() + seconds
        while time.monotonic() < until:
            app.processEvents()
            time.sleep(.005)

    def capture(widget, name, *, final=False):
        widget.show()
        pump()
        if widget is window:
            widget.settings_button.clearFocus()
        path = root / "docs" / "images" / "app-overview.png" if final else output / f"{name}.png"
        path.parent.mkdir(parents=True, exist_ok=True)
        if not widget.grab().save(str(path)):
            raise RuntimeError(f"Could not write {path}")
        print(path, flush=True)

    regions = [RegionConfig(id="studio1", name="画面 1", rect=(.036, .16, .443, .62)),
               RegionConfig(id="studio2", name="画面 2", rect=(.53, .16, .44, .62))]
    config = AppConfig(source="演示视频 · 双画面", wechat_enabled=True, regions=regions)
    with tempfile.TemporaryDirectory(prefix="vlc-monitor-ui-preview-") as directory:
        # No VLC backend is created without an explicit connect command.
        window = MainWindow(None, config, data_dir=directory, wechat_factory=ActualPreviewWechat)
        try:
            window.preview_timer.stop()
            window.alert_timer.stop()
            window.cleanup_timer.stop()
            pump(.15)
            window.statusBar().clearMessage()
            status = dict(state_version=999, playback_state="playing", running=True,
                          session="isolated-preview-revised", run_id=1, can_monitor=True,
                          statuses={"studio1": "正常", "studio2": "正常"})
            window._playing_source = config.source
            window.on_status(status)
            window.canvas.set_frame(revised_frame())
            window.select_region("studio1")
            now = datetime(2026, 10, 16, 10, 26, 4).timestamp()
            recovered = AlarmEvent("preview-recovered", status["session"], "studio2", "画面 2",
                                   "freeze", 0, now - 120, now - 110, now - 90, 10,
                                   elapsed_seconds=30, ended_at=now - 90,
                                   end_reason="正常恢复", run_id=1)
            window.on_events([recovered])
            window.on_wechat_delivery(dict(event_ids=[recovered.id], phase="recovery",
                                          result="accepted", sequence=1, attempts=1))
            capture(window, "normal")
            capture(window, "app-overview", final=True)
            window.resize(920, 640)
            capture(window, "minimum")
            window.resize(1240, 820)
            window.on_status(dict(status, state_version=1000,
                                  statuses={"studio1": "正常", "studio2": {"freeze": "告警"}}))
            alarm = AlarmEvent("preview-active", status["session"], "studio2", "画面 2",
                               "freeze", 0, now - 10, now, now, 10,
                               elapsed_seconds=10, run_id=1)
            window.on_events([alarm])
            capture(window, "alarm")
            window.acknowledge_alarms()
            capture(window, "acknowledged")
            settings = SettingsDialog(window.config, window.wechat, window,
                                      apply_local=window.apply_local_settings)
            capture(settings, "settings-general")
            settings.tabs.setCurrentIndex(1)
            capture(settings, "wechat-bound")
            window.wechat.snapshot["result"] = "network_failed"
            window.wechat.updated.emit(window.wechat.snapshot)
            capture(settings, "wechat-failed")
            settings.close()
            window.on_status(dict(status, state_version=1001, running=False,
                                  playback_state="disconnected", can_monitor=False,
                                  statuses={}))
            window.config.regions = []
            window.selected_id = ""
            window.refresh_regions()
            window.canvas.set_frame(None)
            window.history = {}
            window.refresh_history()
            window._playing_source = ""
            window.source.clear()
            window.update_source_hint()
            window.render_controls()
            capture(window, "empty")
        finally:
            # Use the production asynchronous shutdown path; never terminate threads.
            window.status_data["running"] = False
            window.dirty = False
            window.close()
            deadline = time.monotonic() + 8
            while (window.thread.isRunning() or not window.event_store.is_closed) and time.monotonic() < deadline:
                pump(.02)
            pump(.05)
            if window.thread.isRunning() or not window.event_store.is_closed:
                raise RuntimeError("Preview cleanup did not finish; no thread was killed")
            print("Actual UI preview: monitor thread and temporary event store closed cleanly", flush=True)


def revised_frame():
    """Draw two neutral studio feeds without external media or personal data."""
    image = QImage(1280, 720, QImage.Format.Format_RGB32)
    painter = QPainter(image)
    painter.fillRect(image.rect(), QColor("#182330"))
    for x, title, dark, light in (
        (20, "STUDIO 01", "#1c3547", "#627e8a"),
        (650, "STUDIO 02", "#25313e", "#72828c"),
    ):
        gradient = QLinearGradient(x, 90, x + 610, 570)
        gradient.setColorAt(0, QColor(light))
        gradient.setColorAt(1, QColor(dark))
        painter.fillRect(x, 80, 610, 510, gradient)
        painter.fillRect(x, 435, 610, 155, QColor(dark))
        painter.fillRect(x + 28, 120, 554, 245, QColor("#152b3b"))
        painter.setPen(QColor("#8098a7"))
        painter.setFont(QFont("Helvetica Neue", 29))
        painter.drawText(x + 58, 195, title)
        painter.setFont(QFont("Helvetica Neue", 64, QFont.Weight.Light))
        painter.setPen(QColor("#dfebf1"))
        painter.drawText(x + 58, 302, "10:26:04")
        painter.setPen(QColor("#b9cad4"))
        painter.setFont(QFont("PingFang SC", 19))
        painter.drawText(x + 30, 630, "演示信号 · 合成画面")
    painter.end()
    pixels = np.frombuffer(image.bits(), dtype=np.uint8).reshape(720, 1280, 4).copy()
    pixels.flags.writeable = False
    return FrameSample("isolated-preview-revised", 1, 0, pixels, 16 / 9)


def revised_main_preview():
    """Keep the original workbench hierarchy; simplify entry points only."""
    def quiet_action(text):
        control = button(text)
        control.setFlat(True)
        control.setStyleSheet("QPushButton { background:transparent; border:none; padding:5px 8px; color:#425f76; } QPushButton:hover { background:#e2e9ee; }")
        return control

    window = QMainWindow()
    window.setWindowTitle("VLC / 监播工作台")
    window.setMinimumSize(920, 640)
    center = QWidget()
    window.setCentralWidget(center)
    layout = QVBoxLayout(center)
    layout.setContentsMargins(20, 16, 20, 12)
    layout.setSpacing(10)
    heading = QHBoxLayout()
    heading.addWidget(label("VLC / 监播工作台", "brand"))
    heading.addWidget(label("本地监播 · 告警与恢复", "muted"))
    heading.addStretch()
    heading.addWidget(quiet_action("设置"))
    heading.addWidget(quiet_action("保存配置"))
    layout.addLayout(heading)
    connection = QHBoxLayout()
    source = QLineEdit("演示视频 · 双画面")
    browse = source.addAction(window.style().standardIcon(QStyle.StandardPixmap.SP_DirOpenIcon),
                              QLineEdit.ActionPosition.TrailingPosition)
    browse.setToolTip("本地视频")
    source.setAccessibleDescription("视频来源；右侧文件夹图标选择本地视频")
    connection.addWidget(source, 1)
    connection.addWidget(button("停止播放"))
    layout.addLayout(connection)
    tools = QHBoxLayout()
    tools.addWidget(button("停止监播", True))
    tools.addWidget(label("2 个区域 · 检测正常", "muted"))
    tools.addStretch()
    layout.addLayout(tools)
    vertical = QSplitter(Qt.Orientation.Vertical)
    horizontal = QSplitter(Qt.Orientation.Horizontal)
    canvas = VideoCanvas()
    canvas.set_frame(revised_frame())
    canvas.set_regions([
        RegionConfig(id="studio1", name="画面 1", rect=(.036, .16, .443, .62)),
        RegionConfig(id="studio2", name="画面 2", rect=(.53, .16, .44, .62)),
    ])
    canvas.set_statuses({"studio1": "正常", "studio2": "正常"})
    canvas.set_selected("studio1")
    horizontal.addWidget(canvas)
    sidebar = QScrollArea()
    sidebar.setWidgetResizable(True)
    sidebar.setFrameShape(QScrollArea.Shape.NoFrame)
    sidebar.setMinimumWidth(295)
    sidebar.setMaximumWidth(350)
    content = QWidget()
    sidebar.setWidget(content)
    side = QVBoxLayout(content)
    side.setSizeConstraint(QLayout.SizeConstraint.SetMinimumSize)
    side.setContentsMargins(12, 0, 0, 0)
    title = QHBoxLayout()
    title.addWidget(label("监播区域", "section"))
    title.addStretch()
    title.addWidget(quiet_action("＋ 新增区域"))
    side.addLayout(title)
    regions = QListWidget()
    regions.addItems(["● 画面 1    ·    正常", "● 画面 2    ·    正常"])
    regions.setMinimumHeight(95)
    regions.setMaximumHeight(95)
    regions.setCurrentRow(0)
    side.addWidget(regions)
    rules = QGroupBox("区域规则")
    fields = QFormLayout(rules)
    fields.setVerticalSpacing(5)
    name = QLineEdit("画面 1")
    name.setFixedHeight(28)
    fields.addRow("名称", name)
    for title, text in (("状态", "启用此区域"), ("检测", "黑屏"), ("", "疑似静帧")):
        control = QCheckBox(text)
        control.setChecked(True)
        fields.addRow(title, control)
    for title, value in (("黑屏连续次数", 4), ("静帧比较次数", 10), ("恢复连续次数", 3)):
        spin = QSpinBox()
        spin.setRange(1, 3600)
        spin.setValue(value)
        spin.setFixedHeight(28)
        fields.addRow(title, spin)
    side.addWidget(rules)
    actions = QHBoxLayout()
    for text in ("应用规则", "取消编辑"):
        control = button(text)
        control.setEnabled(False)
        actions.addWidget(control)
    actions.addStretch()
    actions.addWidget(button("删除"))
    side.addLayout(actions)
    canvas.setToolTip("拖动移动区域，右下角缩放；尽量避开字幕。")
    side.addStretch()
    horizontal.addWidget(sidebar)
    horizontal.setSizes([875, 325])
    vertical.addWidget(horizontal)
    history = QWidget()
    history_layout = QVBoxLayout(history)
    history_layout.setContentsMargins(0, 6, 0, 0)
    history_layout.addWidget(label("告警与恢复记录"))
    table = QTableWidget(1, 6)
    table.setHorizontalHeaderLabels(["触发时间", "区域", "类型", "命中 / 经过", "状态 / 结束原因", "通知"])
    table.verticalHeader().hide()
    table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
    table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
    table.horizontalHeader().setSectionResizeMode(4, QHeaderView.ResizeMode.Stretch)
    for column, text in enumerate(("10:24:18", "画面 2", "疑似静帧", "10 次 / 10 秒", "恢复正常", "接口接受 · 请核对手机")):
        table.setItem(0, column, QTableWidgetItem(text))
    history_layout.addWidget(table)
    vertical.addWidget(history)
    vertical.setSizes([520, 116])
    layout.addWidget(vertical, 1)
    status = QHBoxLayout()
    status.addWidget(label("播放中 · 监播中 · 声音开启", "muted"))
    status.addWidget(quiet_action("静音 5 分钟"))
    status.addWidget(label("· 微信通知已开启", "muted"))
    status.addStretch()
    layout.addLayout(status)
    window.resize(1240, 820)
    return window


def render():
    app = QApplication.instance() or QApplication([])
    app.setFont(QFont("PingFang SC" if platform.system() == "Darwin" else "Microsoft YaHei", 11))
    app.setStyleSheet(STYLE)
    if "--actual" in sys.argv:
        render_actual(app)
        return
    output = Path(__file__).resolve().parents[1] / "docs" / "images"
    output.mkdir(parents=True, exist_ok=True)

    def capture(widget, name):
        widget.show()
        app.processEvents()
        path = output / f"ui-preview-{name}.png"
        if not widget.grab().save(str(path)):
            raise RuntimeError(f"Could not write {path}")
        print(path)

    if "--revised" in sys.argv:
        revised = revised_main_preview()
        capture(revised, "main-revised")
        revised.hide()
        return

    normal = main_preview()
    capture(normal, "main-normal")
    normal.hide()
    alarm = main_preview(True)
    capture(alarm, "main-alarm")
    alarm.resize(920, 640)
    capture(alarm, "main-minimum")
    alarm.hide()
    from vlc_monitor.ui.settings_dialog import SettingsDialog
    fake = FakeWechatService()
    noop = lambda: None
    settings = SettingsDialog(AppConfig(), fake, test_local=noop,
                              delayed_test_local=noop, choose_vlc=noop,
                              diagnostics=noop, export_config=noop)
    capture(settings, "settings-general")
    settings.tabs.setCurrentIndex(1)
    capture(settings, "wechat-bound")
    fake.snapshot["result"] = "network_failed"
    fake.updated.emit(fake.snapshot)
    capture(settings, "wechat-failed")
    fake.snapshot["result"] = "uncertain"
    fake.updated.emit(fake.snapshot)
    capture(settings, "wechat-uncertain")
    settings.close()


if __name__ == "__main__":
    render()
