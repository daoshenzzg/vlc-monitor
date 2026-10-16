"""Unified settings view with explicit local drafts and immediate WeChat actions."""
from PySide6.QtCore import QSignalBlocker, Qt
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QFormLayout, QGroupBox, QHBoxLayout,
    QLabel, QMessageBox, QPushButton, QSpinBox, QTabWidget, QVBoxLayout, QWidget,
)

from vlc_monitor.ui.wechat_settings import WechatSettingsWidget


class SettingsDialog(QDialog):
    def __init__(self, config, service, parent=None, *, apply_local=None,
                 test_local=None, delayed_test_local=None, choose_vlc=None,
                 diagnostics=None, export_config=None):
        super().__init__(parent)
        self.config = config
        self._apply_local = apply_local
        self._last_tab = 0
        self.setWindowTitle("设置")
        self.resize(560, 520)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
        layout = QVBoxLayout(self)
        self.tabs = QTabWidget()
        layout.addWidget(self.tabs)
        general = QWidget()
        general_layout = QVBoxLayout(general)
        reminders = QGroupBox("本地提醒")
        form = QFormLayout(reminders)
        self.sound = QCheckBox("告警声音")
        self.foreground = QCheckBox("新告警时显示主窗口")
        self.interval = QSpinBox()
        self.interval.setRange(3, 60)
        self.interval.setSuffix(" 秒")
        form.addRow(self.sound)
        form.addRow(self.foreground)
        form.addRow("未确认告警提醒间隔", self.interval)
        test_row = QHBoxLayout()
        self.test_timing = QComboBox()
        self.test_timing.addItems(["立即", "5 秒后"])
        self.test_button = QPushButton("测试提醒")
        test_row.addWidget(self.test_timing)
        test_row.addWidget(self.test_button)
        test_row.addStretch()
        form.addRow("测试时机", test_row)
        test_hint = QLabel("仅测试本地提醒，不发送微信；使用已应用的配置。")
        test_hint.setWordWrap(True)
        form.addRow(test_hint)
        draft_row = QHBoxLayout()
        self.apply_button = QPushButton("应用")
        self.cancel_button = QPushButton("取消")
        draft_row.addWidget(self.apply_button)
        draft_row.addWidget(self.cancel_button)
        draft_row.addStretch()
        form.addRow(draft_row)
        general_layout.addWidget(reminders)
        support = QGroupBox("播放器与支持")
        support_layout = QVBoxLayout(support)
        path_row = QHBoxLayout()
        self.vlc_path = QLabel()
        self.vlc_path.setTextFormat(Qt.TextFormat.PlainText)
        self.vlc_path.setWordWrap(True)
        self.vlc_path.setMinimumWidth(0)
        path_row.addWidget(self.vlc_path, 1)
        change = QPushButton("更改")
        path_row.addWidget(change)
        support_layout.addLayout(path_row)
        path_hint = QLabel("VLC 位置更改在下次启动后生效。")
        support_layout.addWidget(path_hint)
        support_row = QHBoxLayout()
        diagnostic_button = QPushButton("查看诊断")
        export_button = QPushButton("导出脱敏配置")
        support_row.addWidget(diagnostic_button)
        support_row.addWidget(export_button)
        support_row.addStretch()
        support_layout.addLayout(support_row)
        general_layout.addWidget(support)
        general_layout.addStretch()
        self.tabs.addTab(general, "常规")
        self.wechat_page = WechatSettingsWidget(service)
        self.tabs.addTab(self.wechat_page, "微信通知")
        footer = QHBoxLayout()
        note = QLabel("未保存的更改需在主窗口保存配置。")
        note.setWordWrap(True)
        footer.addWidget(note, 1)
        close = QPushButton("关闭")
        footer.addWidget(close)
        layout.addLayout(footer)
        self._reload()
        for signal in (self.sound.toggled, self.foreground.toggled, self.interval.valueChanged):
            signal.connect(self._draft_changed)
        self.apply_button.clicked.connect(self.apply_draft)
        self.cancel_button.clicked.connect(self._reload)
        self.tabs.currentChanged.connect(self._tab_changed)
        close.clicked.connect(self.reject)
        test_local = test_local or getattr(parent, "test_alarm", None)
        delayed_test_local = delayed_test_local or getattr(parent, "delayed_test_alarm", None)
        self.test_button.clicked.connect(lambda: (delayed_test_local if self.test_timing.currentIndex() else test_local)())
        self.test_button.setEnabled(test_local is not None and delayed_test_local is not None)
        choose_vlc = choose_vlc or getattr(parent, "choose_vlc", None)
        def change_path():
            choose_vlc()
            self._refresh_path()
        change.clicked.connect(change_path)
        change.setEnabled(choose_vlc is not None)
        for button, callback, method in ((diagnostic_button, diagnostics, "diagnostics"),
                                          (export_button, export_config, "export_config")):
            callback = callback or getattr(parent, method, None)
            button.setEnabled(callback is not None)
            if callback:
                button.clicked.connect(callback)
        for button in self.findChildren(QPushButton):
            button.setAutoDefault(False)

    def _values(self):
        return dict(sound_enabled=self.sound.isChecked(), foreground_on_alarm=self.foreground.isChecked(),
                    local_alert_interval_seconds=self.interval.value())

    def has_draft(self):
        return any(value != getattr(self.config, key) for key, value in self._values().items())

    def _draft_changed(self, *_):
        dirty = self.has_draft()
        self.apply_button.setEnabled(dirty)
        self.cancel_button.setEnabled(dirty)

    def _refresh_path(self):
        path = self.config.vlc_path or "自动查找"
        self.vlc_path.setText("VLC 位置：" + path)
        self.vlc_path.setToolTip(path)

    def _reload(self):
        self.sound.setChecked(self.config.sound_enabled)
        self.foreground.setChecked(self.config.foreground_on_alarm)
        self.interval.setValue(int(self.config.local_alert_interval_seconds))
        self._refresh_path()
        self._draft_changed()

    def apply_draft(self):
        values = self._values()
        if self._apply_local:
            if self._apply_local(values) is False:
                return False
        else:
            for key, value in values.items():
                setattr(self.config, key, value)
        self._reload()
        return True

    def _resolve_draft(self):
        if not self.has_draft():
            return True
        box = QMessageBox(QMessageBox.Icon.Question, "本地提醒尚未应用",
                          "如何处理本地提醒更改？已执行的微信操作不受影响。", parent=self)
        apply_button = box.addButton("应用更改", QMessageBox.ButtonRole.AcceptRole)
        discard = box.addButton("放弃更改", QMessageBox.ButtonRole.DestructiveRole)
        cancel = box.addButton("继续编辑", QMessageBox.ButtonRole.RejectRole)
        box.setDefaultButton(cancel)
        box.setEscapeButton(cancel)
        box.exec()
        if box.clickedButton() is apply_button:
            return self.apply_draft()
        if box.clickedButton() is discard:
            self._reload()
            return True
        return False

    def _tab_changed(self, index):
        if self._last_tab == 0 and index != 0 and not self._resolve_draft():
            with QSignalBlocker(self.tabs):
                self.tabs.setCurrentIndex(0)
            return
        self._last_tab = index

    def done(self, result):
        if not self._resolve_draft():
            return
        self.wechat_page.detach()
        super().done(result)

    def closeEvent(self, event):
        if not self._resolve_draft():
            event.ignore()
            return
        self.wechat_page.detach()
        super().closeEvent(event)
