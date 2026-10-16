from dataclasses import replace, asdict
from datetime import datetime
import json
from pathlib import Path
import platform
import shutil
import time
import uuid
import threading

from PySide6.QtCore import Qt, QThread, QTimer, Signal, QStandardPaths
from PySide6.QtGui import QFont, QKeySequence
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QFileDialog, QFormLayout, QHBoxLayout, QHeaderView,
    QLabel, QLineEdit, QListWidget, QListWidgetItem, QMainWindow, QMessageBox,
    QPushButton, QSpinBox, QSplitter, QTableWidget, QTableWidgetItem,
    QVBoxLayout, QWidget, QGroupBox, QScrollArea, QLayout, QStyle,
)

from vlc_monitor import __version__
from vlc_monitor.models import AppConfig, AlarmEvent, RegionConfig
from vlc_monitor.monitoring.controller import MonitorWorker
from vlc_monitor.notifications.local import LocalSound
from vlc_monitor.notifications.coordinator import AlarmCoordinator
from vlc_monitor.notifications.wechat_service import WechatNotificationService
from vlc_monitor.platform.attention import request_attention
from vlc_monitor.ui.vlc_picker import choose_vlc_path
from vlc_monitor.platform.vlc_locator import locate, VlcError
from vlc_monitor.storage.config_store import ConfigStore, redacted_config
from vlc_monitor.storage.event_service import EventService
from vlc_monitor.ui.video_canvas import VideoCanvas


LABELS = {"black": "黑屏", "freeze": "疑似静帧", "stream": "视频更新超时", "interruption": "监播中断", "test": "测试告警"}


class MainWindow(QMainWindow):
    commandRequested = Signal(object)
    regionsRequested = Signal(object)
    pauseRequested = Signal(str)
    shutdownRequested = Signal()

    def __init__(self, location, config=None, data_dir=None, config_problem=False, *, wechat_factory=None):
        super().__init__()
        self.location = location
        self.config = config or AppConfig()
        self.data_dir = Path(data_dir or QStandardPaths.writableLocation(QStandardPaths.StandardLocation.AppLocalDataLocation))
        self.config_store = ConfigStore(self.data_dir / "config.json")
        self.config_problem = config_problem
        self.event_store = None
        self.history = {}
        self.selected_id = ""
        self.editing_ids = set()
        self.status_data = {}
        self.dirty = False
        self._closing = False
        self._storage_close_started = False
        self._storage_failed = False
        self._form_loading = False
        self._last_unacknowledged = set()
        self._local_sequences = {}
        self._last_frame_key = None
        self.pending = None
        self._connect_cancel = None
        self._state_version = -1
        self._playing_source = ""
        self.attention = AlarmCoordinator(repeat_seconds=self.config.local_alert_interval_seconds)
        self.setWindowTitle(f"VLC 监播 · {__version__}")
        self.resize(1240, 820)
        self.setMinimumSize(920, 640)
        self._build_ui()
        self.sound = LocalSound(self)
        self.sound.effect.statusChanged.connect(self.on_sound_status)
        self.event_store = EventService(self.data_dir / "history.sqlite3", parent=self)
        self.event_store.loaded.connect(self.on_history_loaded)
        self.event_store.problem.connect(self.on_storage_problem)
        self.event_store.closed.connect(self.on_storage_closed)
        self.event_store.close_failed.connect(self.on_storage_close_failed)
        self.event_store.recent()
        self.refresh_regions()
        self.refresh_history()
        self.wechat = (wechat_factory or WechatNotificationService)(self.data_dir,
            enabled=self.config.wechat_enabled,
            reminders_enabled=self.config.wechat_reminders_enabled,
            repeat_seconds=self.config.wechat_repeat_seconds, parent=self)
        self.wechat.delivery.connect(self.on_wechat_delivery)
        self.wechat.settingsChanged.connect(self.on_wechat_settings)
        self.wechat.stopped.connect(self.on_wechat_stopped)
        self.wechat.updated.connect(self.render_runtime)
        self.thread = QThread(self)
        self.worker = MonitorWorker(location, self.config)
        self.worker.moveToThread(self.thread)
        self.thread.started.connect(self.worker.initialize)
        for signal, slot in ((self.commandRequested, self.worker.execute), (self.regionsRequested, self.worker.update_regions), (self.pauseRequested, self.worker.pause_region), (self.shutdownRequested, self.worker.shutdown)):
            signal.connect(slot)
        self.worker.command_result.connect(self.on_command_result)
        self.worker.status.connect(self.on_status)
        self.worker.events.connect(self.on_events)
        self.worker.error.connect(self.show_error)
        self.worker.finished.connect(self.thread.quit)
        self.worker.shutdown_failed.connect(self.on_shutdown_failed)
        self.worker.finished.connect(self.worker.deleteLater)
        self.thread.finished.connect(self.on_monitor_stopped)
        self.thread.start()
        self.preview_timer = QTimer(self)
        self.preview_timer.timeout.connect(self.refresh_preview)
        self.preview_timer.start(50)
        self.cleanup_timer = QTimer(self)
        self.cleanup_timer.timeout.connect(self.cleanup_history)
        self.cleanup_timer.start(60 * 60 * 1000)
        self.alert_timer = QTimer(self)
        self.alert_timer.timeout.connect(self.dispatch_attention)
        self.alert_timer.start(100)
        self.render_controls()

    def _button(self, text, handler, primary=False):
        button = QPushButton(text)
        button.clicked.connect(handler)
        if primary:
            button.setObjectName("primary")
        return button

    def _quiet_button(self, text, handler):
        button = self._button(text, handler)
        button.setObjectName("quiet")
        button.setFlat(True)
        return button

    def _build_ui(self):
        self.setFont(QFont("PingFang SC" if platform.system() == "Darwin" else "Microsoft YaHei", 11))
        self.setStyleSheet("""
          QMainWindow,QWidget { color:#243447; background:#f1f3f5; }
          QLabel#brand { font-size:23px; font-weight:700; color:#152b40; }
          QLabel#eyebrow { color:#5e7388; font-size:11px; }
          QPushButton { background:#fff; border:1px solid #c6d0da; border-radius:5px; padding:7px 13px; }
          QPushButton:hover { border-color:#688aa6; background:#eaf1f5; }
          QPushButton#quiet { background:transparent; border:none; padding:5px 8px; color:#425f76; }
          QPushButton#quiet:hover { background:#e2e9ee; }
          QPushButton:focus { border:1px solid #688aa6; }
          QPushButton#quiet:focus { border:1px solid #688aa6; padding:4px 7px; }
          QPushButton#primary { background:#bd4b29; color:white; border-color:#bd4b29; font-weight:600; }
          QPushButton:disabled { color:#9da8b3; background:#e3e8ec; border-color:#d5dde3; }
          QPushButton#primary:disabled { color:#87939f; background:#dfe5e9; border-color:#d5dde3; }
          QLineEdit,QSpinBox,QListWidget,QTableWidget { background:#fff; border:1px solid #ccd5dd; border-radius:4px; padding:5px; }
          QListWidget::item { padding:8px 5px; border-bottom:1px solid #edf0f3; }
          QListWidget::item:selected { background:#dce9ef; color:#17374b; }
          QGroupBox { border:1px solid #d1dbe2; border-radius:6px; margin-top:14px; padding:12px 9px 8px; font-weight:600; }
          QGroupBox::title { subcontrol-origin:margin; left:9px; padding:0 4px; }
          QHeaderView::section { background:#e3e9ed; padding:6px; border:none; font-weight:600; }
          QTableWidget { gridline-color:#e4e9ed; }
        """)
        center = QWidget()
        layout = QVBoxLayout(center)
        layout.setContentsMargins(20, 16, 20, 12)
        layout.setSpacing(10)
        heading = QHBoxLayout()
        title = QLabel("VLC / 监播工作台")
        title.setObjectName("brand")
        heading.addWidget(title)
        subtitle = QLabel("本地监播 · 告警与恢复")
        subtitle.setObjectName("eyebrow")
        heading.addWidget(subtitle)
        heading.addStretch()
        self.settings_button = self._quiet_button("设置", self.open_settings)
        heading.addWidget(self.settings_button)
        self.save_button = self._quiet_button("保存配置", self.save_config)
        heading.addWidget(self.save_button)
        layout.addLayout(heading)
        connection = QHBoxLayout()
        self.source = QLineEdit(self.config.source)
        self.source.setPlaceholderText("输入视频地址，或选择本地文件")
        self.source.setAccessibleName("视频来源")
        self.source.setAccessibleDescription("右侧文件夹图标选择本地视频，也可使用打开文件快捷键")
        self.source.textEdited.connect(self.mark_dirty)
        self.source.textChanged.connect(self.update_source_hint)
        self.source.returnPressed.connect(self.play_from_enter)
        connection.addWidget(self.source, 1)
        self.file_action = self.source.addAction(self.style().standardIcon(QStyle.StandardPixmap.SP_DirOpenIcon), QLineEdit.ActionPosition.TrailingPosition)
        self.file_action.setText("本地视频")
        self.file_action.setToolTip("选择本地视频")
        self.file_action.setShortcut(QKeySequence.StandardKey.Open)
        self.file_action.triggered.connect(self.open_file)
        self.connect_button = self._button("播放", self.toggle_playback)
        self.connect_button.setToolTip("停止播放也会停止当前监播")
        connection.addWidget(self.connect_button)
        layout.addLayout(connection)
        self.source_hint = QLabel()
        self.source_hint.setWordWrap(True)
        self.source_hint.hide()
        layout.addWidget(self.source_hint)
        tools = QHBoxLayout()
        self.start_button = self._button("开始监播", self.toggle_monitoring, True)
        tools.addWidget(self.start_button)
        self.monitor_summary = QLabel()
        self.monitor_summary.setObjectName("eyebrow")
        tools.addWidget(self.monitor_summary)
        tools.addStretch()
        layout.addLayout(tools)
        self.alarm_bar = QWidget()
        self.alarm_bar.setStyleSheet("QWidget { background:#a82d27; color:white; border-radius:5px; } QPushButton { color:#86231f; background:white; }")
        alarm_layout = QHBoxLayout(self.alarm_bar)
        alarm_layout.setContentsMargins(12, 6, 12, 6)
        self.alarm_summary = QLabel()
        self.alarm_summary.setTextFormat(Qt.TextFormat.PlainText)
        self.alarm_summary.setWordWrap(True)
        alarm_layout.addWidget(self.alarm_summary, 1)
        self.ack_button = self._button("确认告警", self.acknowledge_alarms)
        alarm_layout.addWidget(self.ack_button)
        self.alarm_bar.hide()
        vertical = QSplitter(Qt.Orientation.Vertical)
        horizontal = QSplitter(Qt.Orientation.Horizontal)
        self.canvas = VideoCanvas()
        self.canvas.regionCreated.connect(self.add_region)
        self.canvas.regionSelected.connect(self.select_region)
        self.canvas.regionEdited.connect(self.edit_rectangle)
        self.canvas.editStarted.connect(self.begin_edit)
        self.canvas.editCancelled.connect(self.cancel_canvas_edit)
        horizontal.addWidget(self.canvas)
        sidebar = QScrollArea()
        sidebar.setWidgetResizable(True)
        sidebar.setFrameShape(QScrollArea.Shape.NoFrame)
        sidebar.setMinimumWidth(295)
        sidebar.setMaximumWidth(350)
        sidebar_content = QWidget()
        sidebar.setWidget(sidebar_content)
        side = QVBoxLayout(sidebar_content)
        side.setSizeConstraint(QLayout.SizeConstraint.SetMinimumSize)
        side.setContentsMargins(12, 0, 0, 0)
        region_title = QHBoxLayout()
        region_title.addWidget(QLabel("监播区域"))
        region_title.addStretch()
        self.add_region_button = self._quiet_button("＋ 新增区域", self.start_drawing)
        region_title.addWidget(self.add_region_button)
        side.addLayout(region_title)
        self.region_list = QListWidget()
        self.region_list.setMinimumHeight(76)
        self.region_list.setMaximumHeight(76)
        self.region_list.currentItemChanged.connect(self.on_region_selection)
        side.addWidget(self.region_list, 1)
        self.region_empty = QLabel("新增区域后，在画面中拖出检测框。")
        self.region_empty.setWordWrap(True)
        side.addWidget(self.region_empty)
        self.form = QGroupBox("区域规则")
        fields = QFormLayout(self.form)
        fields.setVerticalSpacing(3)
        self.name_edit = QLineEdit()
        control_height = max(28, self.fontMetrics().height() + 10)
        self.name_edit.setFixedHeight(control_height)
        self.enabled = QCheckBox("启用此区域")
        self.black = QCheckBox("黑屏")
        self.freeze = QCheckBox("疑似静帧")
        self.black_count = QSpinBox()
        self.freeze_count = QSpinBox()
        self.recovery_count = QSpinBox()
        for spin in (self.black_count, self.freeze_count, self.recovery_count):
            spin.setFixedHeight(control_height)
            spin.setRange(1, 3600)
            spin.valueChanged.connect(self.rule_changed)
        self.name_edit.textEdited.connect(self.rule_changed)
        for check in (self.enabled, self.black, self.freeze):
            check.toggled.connect(self.rule_changed)
        for label, widget in (("名称", self.name_edit), ("状态", self.enabled), ("检测", self.black), ("", self.freeze), ("黑屏连续次数", self.black_count), ("静帧比较次数", self.freeze_count), ("恢复连续次数", self.recovery_count)):
            fields.addRow(label, widget)
        side.addWidget(self.form)
        actions = QHBoxLayout()
        self.apply_button = self._button("应用规则", self.apply_region)
        self.cancel_button = self._button("取消编辑", self.cancel_rule_edit)
        self.delete_button = self._button("删除", self.delete_region)
        actions.addWidget(self.apply_button)
        actions.addWidget(self.cancel_button)
        actions.addStretch()
        actions.addWidget(self.delete_button)
        side.addLayout(actions)
        self.canvas.setToolTip("拖动移动，右下角缩放；尽量避开字幕和时钟，静态画面也可能触发疑似静帧。")
        side.addStretch()
        horizontal.addWidget(sidebar)
        horizontal.setSizes([875, 325])
        vertical.addWidget(horizontal)
        history_panel = QWidget()
        history_layout = QVBoxLayout(history_panel)
        history_layout.setContentsMargins(0, 6, 0, 0)
        history_layout.addWidget(self.alarm_bar)
        history_layout.addWidget(QLabel("告警与恢复记录"))
        self.history_empty = QLabel("暂无告警记录")
        history_layout.addWidget(self.history_empty)
        self.table = QTableWidget(0, 6)
        self.table.setHorizontalHeaderLabels(["触发时间", "区域", "类型", "命中 / 经过", "状态 / 结束原因", "通知"])
        self.table.verticalHeader().hide()
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(4, QHeaderView.ResizeMode.Stretch)
        history_layout.addWidget(self.table)
        vertical.addWidget(history_panel)
        vertical.setSizes([520, 116])
        layout.addWidget(vertical, 1)
        footer = QHBoxLayout()
        self.runtime = QLabel("未连接 · 未监播")
        self.runtime.setWordWrap(False)
        self.mute_button = self._quiet_button("静音 5 分钟", self.toggle_mute)
        self.wechat_status = QLabel("· 微信通知已关闭")
        self.wechat_status.setWordWrap(False)
        footer.addWidget(self.runtime)
        footer.addWidget(self.mute_button)
        footer.addWidget(self.wechat_status)
        footer.addStretch()
        layout.addLayout(footer)
        self.storage_warning = QLabel("部分历史未保存，仅内存有效")
        self.storage_warning.setWordWrap(True)
        self.storage_warning.hide()
        layout.addWidget(self.storage_warning)
        self.setCentralWidget(center)

    def mark_dirty(self, *_):
        self.dirty = True
        self.save_button.setText("保存配置 *")

    def on_history_loaded(self, rows):
        # Startup reads must not overwrite newer in-memory ends/receipts/acks.
        for row in rows:
            current = self.history.get(row["id"])
            if current is None:
                self.history[row["id"]] = row
            else:
                for key in ("acknowledged_at", "delivery"):
                    if key not in current and key in row:
                        current[key] = row[key]
        self.history = dict(sorted(self.history.items(), key=lambda item: item[1]["triggered_at"], reverse=True)[:200])
        self.refresh_history()

    def on_storage_problem(self, message):
        self._storage_failed = True
        self.statusBar().showMessage(message)
        self.storage_warning.show()

    def on_storage_closed(self):
        if self._closing:
            self.close()

    def on_storage_close_failed(self, message):
        if not self._closing:
            return
        dialog = QMessageBox(QMessageBox.Icon.Warning, "部分历史尚未保存", message, parent=self)
        retry = dialog.addButton("重试保存", QMessageBox.ButtonRole.AcceptRole)
        discard = dialog.addButton("放弃未保存历史并退出", QMessageBox.ButtonRole.DestructiveRole)
        dialog.setDefaultButton(retry)
        dialog.setEscapeButton(retry)
        dialog.exec()
        if dialog.clickedButton() is discard:
            self.event_store.discard_close()
        else:
            self.event_store.retry_close()

    def _finish_shutdown(self):
        if not self._closing or self.thread.isRunning() or self.wechat.thread.isRunning():
            return
        if not self._storage_close_started:
            self._storage_close_started = True
            # One queued turn drains already-posted final producer receipts first.
            QTimer.singleShot(0, self._close_storage)

    def _close_storage(self):
        if self._closing:
            self.event_store.begin_close()

    def selected_region(self):
        return next((r for r in self.config.regions if r.id == self.selected_id), None)

    def refresh_regions(self):
        self.region_list.blockSignals(True)
        self.region_list.clear()
        for region in self.config.regions:
            item = QListWidgetItem(f"{'●' if region.enabled else '○'} {region.name}   ·   黑 {region.black_count} / 静 {region.freeze_count}")
            item.setData(Qt.ItemDataRole.UserRole, region.id)
            self.region_list.addItem(item)
            if region.id == self.selected_id:
                self.region_list.setCurrentItem(item)
        self.region_list.blockSignals(False)
        self.canvas.set_regions(self.config.regions)
        self.canvas.set_selected(self.selected_id)
        self.load_form()

    def load_form(self):
        region = self.selected_region()
        self.form.setVisible(region is not None)
        self.region_empty.setVisible(region is None)
        self.region_empty.setText("选择一个区域查看规则。" if self.config.regions else "新增区域后，在画面中拖出检测框。")
        self.apply_button.setEnabled(bool(region and region.id in self.editing_ids))
        self.cancel_button.setEnabled(bool(region and region.id in self.editing_ids))
        self.delete_button.setEnabled(region is not None)
        for button in (self.apply_button, self.cancel_button, self.delete_button):
            button.setVisible(region is not None)
        if region:
            self._form_loading = True
            self.name_edit.setText(region.name)
            for widget, value in ((self.enabled, region.enabled), (self.black, region.black_enabled), (self.freeze, region.freeze_enabled)):
                widget.setChecked(value)
            for widget, value in ((self.black_count, region.black_count), (self.freeze_count, region.freeze_count), (self.recovery_count, region.recovery_count)):
                widget.setValue(value)
            self._form_loading = False

    def on_region_selection(self, item, previous):
        if item:
            self.select_region(item.data(Qt.ItemDataRole.UserRole))

    def select_region(self, region_id):
        if self.selected_id != region_id and self.selected_id in self.editing_ids:
            self.cancel_rule_edit()
        self.selected_id = region_id
        self.refresh_regions()

    def publish_regions(self):
        # Never share mutable config objects with the worker's queued command.
        self.regionsRequested.emit([replace(r) for r in self.config.regions])
        self.refresh_regions()
        self.mark_dirty()
        self.sync_attention_context()
        self.render_controls()

    def add_region(self, rect):
        if self.editing_ids:
            return self.show_error("请先应用或取消当前区域的编辑，再新增区域。")
        if len(self.config.regions) >= 64:
            return self.show_error("最多支持 64 个区域。")
        region = RegionConfig(name=f"区域 {len(self.config.regions) + 1}", rect=tuple(rect))
        self.config.regions.append(region)
        self.selected_id = region.id
        self.canvas.set_drawing(False)
        self.publish_regions()

    def begin_edit(self, region_id):
        if region_id not in self.editing_ids:
            self.editing_ids.add(region_id)
            self.pauseRequested.emit(region_id)
            self.sync_attention_context()
            self.render_controls()
            self.apply_button.setEnabled(self.selected_id == region_id)
            self.cancel_button.setEnabled(self.selected_id == region_id)
            self.statusBar().showMessage("区域编辑中：该区域检测已暂停；应用后重新预热。")

    def rule_changed(self, *_):
        if not self._form_loading and self.selected_id:
            self.begin_edit(self.selected_id)

    def cancel_canvas_edit(self, region_id):
        if region_id in self.editing_ids:
            self.editing_ids.remove(region_id)
            self.config.regions = [replace(r, revision=r.revision + 1) if r.id == region_id else r for r in self.config.regions]
            self.publish_regions()

    def cancel_rule_edit(self):
        self.cancel_canvas_edit(self.selected_id)
        self.load_form()

    def apply_region(self):
        region = self.selected_region()
        if region is None:
            return
        updated = replace(region, name=self.name_edit.text().strip(), enabled=self.enabled.isChecked(), black_enabled=self.black.isChecked(), freeze_enabled=self.freeze.isChecked(), black_count=self.black_count.value(), freeze_count=self.freeze_count.value(), recovery_count=self.recovery_count.value(), revision=region.revision + 1)
        try:
            updated.validate()
        except ValueError as exc:
            self.show_error(str(exc))
            return
        self.config.regions = [updated if r.id == region.id else r for r in self.config.regions]
        self.editing_ids.discard(region.id)
        self.publish_regions()
        self.statusBar().showMessage("规则已应用，受影响区域重新预热；点击保存配置可下次加载。", 5000)

    def edit_rectangle(self, region_id, rect):
        self.config.regions = [replace(r, rect=tuple(rect), revision=r.revision + 1) if r.id == region_id else r for r in self.config.regions]
        self.editing_ids.discard(region_id)
        self.publish_regions()

    def delete_region(self):
        if not self.selected_region():
            return
        dialog = QMessageBox(QMessageBox.Icon.Warning, "删除区域", "删除所选监播区域？已有告警记录会保留。", parent=self)
        remove = dialog.addButton("删除区域", QMessageBox.ButtonRole.DestructiveRole)
        cancel = dialog.addButton("取消", QMessageBox.ButtonRole.RejectRole)
        dialog.setDefaultButton(cancel)
        dialog.setEscapeButton(cancel)
        dialog.exec()
        if dialog.clickedButton() is not remove:
            return
        region_id = self.selected_id
        self.config.regions = [r for r in self.config.regions if r.id != region_id]
        self.editing_ids.discard(region_id)
        self.selected_id = ""
        self.publish_regions()

    def open_file(self):
        path, _ = QFileDialog.getOpenFileName(self, "选择本地视频")
        if path:
            self.source.setText(path)
            self.mark_dirty()

    def start_drawing(self):
        if self.editing_ids:
            return self.show_error("请先应用或取消当前区域编辑。")
        self.canvas.set_drawing(True)

    def play_from_enter(self):
        if not self.pending and self.status_data.get("playback_state", "disconnected") in ("disconnected", "error") and not self.status_data.get("running"):
            self.connect_source()

    def update_source_hint(self, *_):
        changed = bool(self._playing_source and self.source.text().strip() != self._playing_source)
        self.source_hint.setText("新地址将在停止后播放" if changed else "")
        self.source_hint.setVisible(changed)

    def submit_command(self, action, **payload):
        if self._closing or self.pending:
            return
        request = {"request_id": str(uuid.uuid4()), "action": action, **payload}
        self.pending = request
        self.sync_attention_context()
        self.render_controls()
        self.commandRequested.emit(request)

    def toggle_playback(self):
        if self.pending:
            if self.pending["action"] == "connect":
                self.cancel_connection()
            return
        state = self.status_data.get("playback_state", "disconnected")
        if state in ("playing", "cleanup_error") or self.status_data.get("running"):
            self.stop_playback()
        else:
            self.connect_source()

    def cancel_connection(self):
        if not self.pending or self.pending["action"] != "connect":
            return
        target = self.pending["request_id"]
        self._connect_cancel.set()
        self.pending = {"request_id": str(uuid.uuid4()), "action": "cancel", "target": target}
        self.commandRequested.emit(self.pending)
        self.render_controls()

    def connect_source(self):
        if self.pending or self._closing:
            return
        if self.status_data.get("playback_state") in ("playing", "cleanup_error", "stopping") or self.status_data.get("running"):
            return
        source = self.source.text().strip()
        if not source:
            self.source_hint.setText("请输入视频地址，或选择本地文件。")
            self.source_hint.show()
            self.source.setFocus()
            return
        self.cancel_rule_edit()
        self.attention.set_context("", False, self.config.regions)
        self.refresh_alarm_bar()
        self.canvas.set_frame(None)
        self.canvas.set_drawing(False)
        self._last_frame_key = None
        self._connect_cancel = threading.Event()
        self.submit_command("connect", source=source, cancel=self._connect_cancel)
        self.statusBar().showMessage("正在连接，可取消。")

    def stop_playback(self):
        if not self.pending:
            self.cancel_rule_edit()
            self.canvas.set_drawing(False)
            self.submit_command("disconnect")

    def toggle_monitoring(self):
        if self.pending:
            return
        if self.status_data.get("running"):
            self.submit_command("stop")
        else:
            self.start_monitoring()

    def start_monitoring(self):
        if self.editing_ids:
            return self.show_error("请先应用或取消正在编辑的区域。")
        self.submit_command("start")

    def on_command_result(self, result):
        # A late connect reply cannot release a newer cancel request.
        if not self.pending or result["request_id"] != self.pending["request_id"]:
            return
        request = self.pending
        self.pending = None
        if result["success"]:
            if request["action"] == "connect":
                self._playing_source = request["source"]
            elif request["action"] in ("disconnect", "cancel"):
                self._playing_source = ""
        self.update_source_hint()
        self.sync_attention_context()
        self.render_controls()
        if not result["success"] and not self._closing:
            self.statusBar().showMessage(result["reason"] or "操作失败")

    def render_controls(self):
        data = self.status_data
        action = self.pending["action"] if self.pending else ""
        state = data.get("playback_state", "disconnected")
        running = data.get("running", False)
        labels = {"connect": "取消连接", "cancel": "正在取消…", "disconnect": "正在停止…"}
        self.connect_button.setText(labels.get(action, "重试停止" if state == "cleanup_error" else "停止播放" if state == "playing" or running else "重试播放" if state == "error" else "播放"))
        self.connect_button.setEnabled(not action or action == "connect")
        self.start_button.setText("正在开始…" if action == "start" else "正在停止…" if action == "stop" else "停止监播" if running else "开始监播")
        self.start_button.setEnabled(not action and (running or (data.get("can_monitor", False) and not self.editing_ids)))
        self.start_button.setToolTip("" if running else "请先应用或取消区域编辑" if self.editing_ids else data.get("cannot_monitor_reason", "缺少有效视频"))
        self.source.setEnabled(not action)
        self.file_action.setEnabled(not action)
        self.add_region_button.setEnabled(not action and state == "playing" and not self.editing_ids)
        self.monitor_summary.setText(f"{len(self.config.regions)} 个区域 · " + ("区域编辑中" if self.editing_ids else "监播中" if running else "播放后开始监播"))

    def sync_attention_context(self):
        cancelling = self.pending and self.pending["action"] in ("connect", "cancel", "disconnect", "stop")
        self.attention.set_suspended(bool(cancelling))
        self.attention.set_context(self.status_data.get("session", ""), self.status_data.get("running", False) and not self._closing,
            self.config.regions, self.editing_ids, run_id=self.status_data.get("run_id", 0))
        if hasattr(self, "wechat"):
            self.wechat.set_context(self.status_data.get("session", ""),
                self.status_data.get("running", False) and not self._closing and not cancelling,
                self.config.regions, self.editing_ids, run_id=self.status_data.get("run_id", 0))
        self.refresh_alarm_bar()

    def on_status(self, data):
        if data.get("state_version", 0) <= self._state_version:
            return
        self._state_version = data.get("state_version", 0)
        self.status_data = data
        self.sync_attention_context()
        self.canvas.set_statuses(data["statuses"])
        self.render_controls()
        self.render_runtime()

    def render_runtime(self, *_):
        data = self.status_data
        muted = self.sound.policy.muted_until > time.monotonic()
        self.mute_button.setText("取消静音" if muted else "静音 5 分钟")
        self.mute_button.setEnabled(self.config.sound_enabled or muted)
        self.mute_button.setToolTip("仅影响本地声音，不影响微信" if self.config.sound_enabled else "声音已在设置中关闭")
        state = {"disconnected": "未连接", "opening": "正在打开", "waiting": "等待有效视频", "playing": "播放中", "error": "播放失败", "stopping": "正在停止", "cleanup_error": "播放器释放失败"}.get(data.get("playback_state", "disconnected"), "未连接")
        sound = f"声音静音（剩余 {max(1, int(self.sound.policy.muted_until - time.monotonic()))} 秒）" if muted and self.config.sound_enabled else "声音开启" if self.config.sound_enabled else "声音关闭"
        self.runtime.setText(f"{state} · {'监播中' if data.get('running') else '未监播'} · {sound}")
        self.storage_warning.setVisible(self._storage_failed)
        snapshot = self.wechat.snapshot
        wechat = "微信通知已关闭" if not snapshot.get("enabled") else "微信通知已开启" if snapshot.get("real_alarms_available") else "微信通知待绑定/检查"
        self.wechat_status.setText("· " + wechat)

    def refresh_preview(self):
        frame = self.worker.frames.latest()
        key = (frame.session_id, frame.frame_seq) if frame else None
        if key != self._last_frame_key:
            self.canvas.set_frame(frame)
            self._last_frame_key = key
        valid = self.worker.valid_frames.latest()
        stale = frame is not None and (valid is None or time.monotonic() - valid.received_at >= self.config.update_timeout)
        self.canvas.set_stale(stale)

    def on_events(self, events):
        accepted = self.attention.ingest(events) if not self._closing else []
        for item in accepted:
            if item.phase == "interruption":
                self.statusBar().showMessage("监播发生调度中断：已重新预热；请检查设备休眠/负载。", 8000)
        archived = []
        accepted_ids = {item.event.id for item in accepted}
        for event in events:
            if event.ended_at is None and event.id not in accepted_ids:
                continue
            previous = self.history.get(event.id, {})
            if previous.get("ended_at") is not None and event.ended_at is None:
                continue
            row = asdict(event)
            if event.id in self.attention.acknowledged_at:
                row["acknowledged_at"] = self.attention.acknowledged_at[event.id]
            for key in ("acknowledged_at", "delivery"):
                if key in previous:
                    row[key] = previous[key]
            self.history[event.id] = row
            if event.ended_at is not None:
                self._local_sequences.pop(event.id, None)
            archived.append(event)
        if self.event_store:
            try:
                self.event_store.upsert(archived)
            except Exception:
                self.statusBar().showMessage("历史写入失败，本次状态仍在内存中；检测和提醒继续。")
        self.history = dict(sorted(self.history.items(), key=lambda item: item[1]["triggered_at"], reverse=True)[:200])
        if not self._closing:
            self.wechat.publish(accepted)
        self.refresh_history()
        self.refresh_alarm_bar()

    def refresh_alarm_bar(self):
        active = self.attention.active
        unacknowledged = [key for key in active if key not in self.attention.acknowledged_at]
        self.alarm_bar.setVisible(bool(active) and not self._closing)
        summaries = [f"{event.region_name or '视频流'} · {LABELS.get(event.anomaly_type, event.anomaly_type)}" for event in list(active.values())[:3]]
        self.alarm_summary.setText(f"{len(active)} 项异常，{len(unacknowledged)} 项未确认" + (" · 已确认，异常持续" if active and not unacknowledged else ""))
        self.alarm_summary.setToolTip("；".join(summaries))
        self.alarm_bar.setStyleSheet("QWidget { background:#f4e8db; color:#6b451f; border-radius:5px; } QPushButton { background:white; }" if active and not unacknowledged else "QWidget { background:#a82d27; color:white; border-radius:5px; } QPushButton { color:#86231f; background:white; }")
        self.ack_button.setEnabled(bool(unacknowledged))
        if self._last_unacknowledged and not unacknowledged and hasattr(self, "sound"):
            self.sound.effect.stop()
        self._last_unacknowledged = set(unacknowledged)

    def dispatch_attention(self):
        if self._closing:
            return
        batches = self.attention.tick()
        if not batches:
            return
        first = any(batch.phase == "first" for batch in batches)
        request_attention(self, foreground=first and self.config.foreground_on_alarm)
        if self.config.sound_enabled:
            result = self.sound.play()
            outcome = "sound_failed" if "失败" in result else "muted" if "限频" in result else "requested"
        else:
            result, outcome = "声音关闭；视觉提醒仍有效", "sound_disabled"
        self.statusBar().showMessage(result, 5000)
        for batch in batches:
            ids = [event.id for event in batch.events]
            for event_id in ids:
                if event_id in self.history:
                    self.history[event_id]["delivery"] = {"channel": "local", "result": outcome}
            if self.event_store:
                for event in batch.events:
                    sequence = self._local_sequences.get(event.id, 0) + (batch.phase == "repeat")
                    if event.ended_at is None:
                        self._local_sequences[event.id] = sequence
                    try:
                        self.event_store.record_delivery([event.id], "local", "test" if event.anomaly_type == "test" else batch.phase, outcome, sequence=sequence)
                    except Exception:
                        self.statusBar().showMessage("通知结果未持久化；本地提醒已独立执行。")
        self.refresh_history()

    def acknowledge_alarms(self):
        when = time.time()
        ids = self.attention.acknowledge(when)
        for event_id in ids:
            if event_id in self.history:
                self.history[event_id]["acknowledged_at"] = when
        self.sound.effect.stop()
        self.refresh_alarm_bar()
        self.refresh_history()
        if self.event_store and ids:
            try:
                self.event_store.acknowledge(ids, when)
            except Exception:
                self.statusBar().showMessage("已在本次运行确认，但确认记录未持久化。")

    def refresh_history(self):
        rows = sorted(self.history.values(), key=lambda e: e["triggered_at"], reverse=True)[:200]
        self.table.setRowCount(len(rows))
        self.history_empty.setVisible(not rows)
        self.table.setVisible(bool(rows))
        for index, event in enumerate(rows):
            if event["end_reason"]:
                state = "恢复正常" if event["end_reason"] == "正常恢复" else event["end_reason"]
            elif event["id"] not in self.attention.active:
                state = "历史记录，当前未监播"
            else:
                state = "已确认，异常持续" if event.get("acknowledged_at") is not None else "告警中 / 待确认"
            notification = event.get("delivery")
            delivery_labels = {"requested": "已请求本地提醒", "sound_disabled": "声音关闭", "muted": "静音/限频", "sound_failed": "音效失败", "accepted": "接口接受（非送达证明）", "failed": "失败", "uncertain": "结果不确定", "queue_full": "队列满，未发送", "expired": "已过期，未继续发送", "cancelled": "已取消（在途不可撤回）"}
            notice = delivery_labels.get(notification["result"], notification["result"]) if notification else event.get("notification_result") or "本地记录 / 旧版未记录通知结果"
            if notification:
                notice = ("微信：" if notification["channel"] == "wechat" else "本地：") + notice
            values = [datetime.fromtimestamp(event["triggered_at"]).strftime("%m-%d %H:%M:%S"), event["region_name"], LABELS.get(event["anomaly_type"], event["anomaly_type"]), f"{event['hit_count']} 次 / {event['elapsed_seconds']:.1f} 秒", state, notice]
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                item.setToolTip(value)
                self.table.setItem(index, column, item)

    def on_sound_status(self):
        from PySide6.QtMultimedia import QSoundEffect
        if self.sound.effect.status() == QSoundEffect.Status.Error:
            self.statusBar().showMessage("音效加载失败：视觉/前台请求与检测继续；请检查音频设备。")

    def toggle_mute(self):
        if self.sound.policy.muted_until > time.monotonic():
            self.sound.policy.unmute()
        else:
            self.sound.policy.mute(time.monotonic())
            self.sound.effect.stop()
        self.render_runtime()

    def test_alarm(self):
        if self._closing:
            return
        now = time.time()
        event = AlarmEvent(str(uuid.uuid4()), "test", "", "测试（非真实异常，不发微信）", "test", 0, now, now, now, 0, ended_at=now, end_reason="测试，不改变真实监播状态")
        self.on_events([event])

    def delayed_test_alarm(self):
        self.statusBar().showMessage("5 秒后测试本地告警：现在可切到其他应用或最小化；不会发送微信。", 5000)
        QTimer.singleShot(5000, self.test_alarm)

    def open_settings(self, tab=0):
        from vlc_monitor.ui.settings_dialog import SettingsDialog
        dialog = SettingsDialog(self.config, self.wechat, self, apply_local=self.apply_local_settings)
        dialog.tabs.setCurrentIndex(tab)
        dialog.exec()

    def apply_local_settings(self, values):
        for key in ("sound_enabled", "foreground_on_alarm", "local_alert_interval_seconds"):
            setattr(self.config, key, values[key])
        self.attention.repeat_seconds = self.config.local_alert_interval_seconds
        if not self.config.sound_enabled:
            self.sound.effect.stop()
        self.mark_dirty()
        self.render_runtime()
        return True

    def on_wechat_settings(self, changes):
        for key, value in changes.items():
            if key in ("wechat_enabled", "wechat_reminders_enabled", "wechat_repeat_seconds"):
                setattr(self.config, key, value)
        self.mark_dirty()

    def on_wechat_delivery(self, result):
        if self.event_store:
            try:
                self.event_store.record_delivery(result["event_ids"], "wechat", result["phase"],
                    result["result"], sequence=result["sequence"], attempts=result["attempts"])
            except Exception:
                self.statusBar().showMessage("微信结果未能保存；本地检测和提醒继续。")
        for event_id in result["event_ids"]:
            if event_id in self.history:
                self.history[event_id]["delivery"] = {"channel": "wechat", "result": result["result"], "created_at": time.time()}
        self.refresh_history()

    def on_wechat_stopped(self):
        if self._closing:
            self._finish_shutdown()

    def on_monitor_stopped(self):
        if self._closing:
            self.wechat.shutdown()
            self._finish_shutdown()

    def save_config(self):
        if self.editing_ids:
            self.show_error("请先应用或取消区域编辑再保存。")
            return False
        self.config.source = self.source.text().strip()
        try:
            if self.config_problem and self.config_store.path.exists():
                if QMessageBox.question(self, "保留原配置", "原配置无效。是否先备份原文件，再保存当前配置？") != QMessageBox.StandardButton.Yes:
                    return False
                backup = self.config_store.path.with_name(f"config.invalid-{time.time_ns()}.json")
                shutil.copy2(self.config_store.path, backup)
            self.config_store.save(self.config)
        except (OSError, ValueError):
            self.show_error("配置保存失败，请检查用户数据目录权限和字段范围；原配置未主动删除。")
            return False
        self.config_problem = False
        self.dirty = False
        self.save_button.setText("保存配置")
        self.statusBar().showMessage("配置已原子保存。完整地址仅保存于本地，不进入诊断导出。", 5000)
        return True

    def choose_vlc(self):
        path = choose_vlc_path(self, self.config.vlc_path or str(self.location.root))
        if not path:
            return
        try:
            locate(path)
        except VlcError as exc:
            return self.show_error(str(exc))
        self.config.vlc_path = path
        self.mark_dirty()
        self.statusBar().showMessage("VLC 位置已更改；保存配置并重启后生效。", 8000)

    def export_config(self):
        path, _ = QFileDialog.getSaveFileName(self, "导出配置（媒体地址将移除）", "VLCMonitor-redacted.json", "JSON (*.json)")
        if not path:
            return
        try:
            Path(path).write_text(json.dumps(redacted_config(self.config), ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError:
            self.show_error("导出失败，请选择可写目录。")

    def diagnostics(self):
        import cv2
        import numpy
        from PySide6.QtCore import qVersion
        from vlc_monitor.platform.vlc_locator import load_binding
        frame = self.worker.frames.latest()
        wechat = {key: self.wechat.snapshot[key] for key in ("state", "result", "enabled", "pending_count", "real_alarms_available")}
        data = {"app": __version__, "system": platform.platform(), "architecture": platform.machine(), "python": platform.python_version(), "qt": qVersion(), "numpy": numpy.__version__, "opencv": cv2.__version__, "vlc": load_binding(self.location).libvlc_get_version().decode(), "vlc_library": str(self.location.library), "vlc_plugins": str(self.location.plugins), "vlc_next_start": self.config.vlc_path, "frame_seq": frame.frame_seq if frame else None, "frame_age_seconds": round(time.monotonic() - frame.received_at, 2) if frame else None, "status": self.status_data.get("problem", ""), "data_directory": str(self.data_dir), "wechat": wechat, "sync_boundaries": self.worker.command_durations}
        text = json.dumps(data, ensure_ascii=False, indent=2)
        QApplication.clipboard().setText(text)
        QMessageBox.information(self, "诊断已复制（不含媒体地址）", text)

    def cleanup_history(self):
        if self.event_store:
            try:
                self.event_store.cleanup()
            except Exception:
                self.statusBar().showMessage("历史清理失败，请检查数据目录权限。")

    def show_error(self, message):
        self.statusBar().showMessage(message)
        QMessageBox.warning(self, "操作提示", message)

    def on_shutdown_failed(self, message):
        self._closing = False
        self.setEnabled(True)
        self.attention = AlarmCoordinator(repeat_seconds=self.config.local_alert_interval_seconds)
        self.sync_attention_context()
        self.alert_timer.start(100)
        self.cleanup_timer.start(60 * 60 * 1000)
        self.statusBar().showMessage(message)
        QMessageBox.warning(self, "播放器尚未安全关闭", message + "\n微信发送已停止，可在设置中手动重新启用。")

    def closeEvent(self, event):
        if self._closing:
            if self.thread.isRunning() or self.wechat.thread.isRunning() or not self.event_store.is_closed:
                self._finish_shutdown()
                event.ignore()
                return
            self.preview_timer.stop()
            event.accept()
            return
        if self.status_data.get("running"):
            dialog = QMessageBox(QMessageBox.Icon.Question, "退出即停止监播", "退出会停止播放、监播和微信通知。", parent=self)
            quit_button = dialog.addButton("停止并退出", QMessageBox.ButtonRole.AcceptRole)
            cancel = dialog.addButton("取消", QMessageBox.ButtonRole.RejectRole)
            dialog.setDefaultButton(cancel)
            dialog.setEscapeButton(cancel)
            dialog.exec()
            if dialog.clickedButton() is not quit_button:
                event.ignore()
                return
        if self.dirty:
            dialog = QMessageBox(QMessageBox.Icon.Question, "保存配置", "是否保存当前配置？不保存不会撤回已执行的微信操作。", parent=self)
            save = dialog.addButton("保存并退出", QMessageBox.ButtonRole.AcceptRole)
            discard = dialog.addButton("不保存退出", QMessageBox.ButtonRole.DestructiveRole)
            cancel = dialog.addButton("取消", QMessageBox.ButtonRole.RejectRole)
            dialog.setDefaultButton(cancel)
            dialog.setEscapeButton(cancel)
            dialog.exec()
            answer = dialog.clickedButton()
            if answer not in (save, discard) or (answer is save and not self.save_config()):
                event.ignore()
                return
        self._closing = True
        self.attention.shutdown()
        self.wechat.quiesce()
        self.alert_timer.stop()
        self.cleanup_timer.stop()
        self.sound.effect.stop()
        if self._connect_cancel:
            self._connect_cancel.set()
        self.setEnabled(False)
        self.statusBar().showMessage("正在停止播放器与微信通知；若有系统凭证提示，请先处理或取消…")
        self.shutdownRequested.emit()
        event.ignore()
