"""The settings window only observes the application-owned notification service."""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import QApplication

from vlc_monitor.ui.wechat_settings import WechatSettingsDialog, WechatSettingsWidget


class FakeService(QObject):
    updated = Signal(object)

    def __init__(self):
        super().__init__()
        self.commands = []
        self.configurations = []
        self.reminders_enabled = False
        self.repeat_seconds = 300
        self.snapshot = dict(
            state="trial_sendable", busy=False, enabled=True,
            deletion_failed=False, test_accepted=True, phone_confirmed=True,
            result="alarm_api_accepted", challenge="", candidate="",
            qr_content="", real_alarms_available=True, pending_count=2,
        )

    def command(self, action):
        self.commands.append(action)

    def configure(self, reminders_enabled, repeat_seconds):
        self.reminders_enabled = reminders_enabled
        self.repeat_seconds = repeat_seconds
        self.configurations.append((reminders_enabled, repeat_seconds))
        self.updated.emit(self.snapshot)


@pytest.mark.parametrize("method", ["close", "reject", "accept"])
def test_closing_settings_detaches_without_disabling_service(method):
    app = QApplication.instance() or QApplication([])
    service = FakeService()
    dialog = WechatSettingsDialog(service)
    dialog.show()
    app.processEvents()
    assert not service.commands
    assert not service.configurations
    getattr(dialog, method)()
    assert dialog._closing
    assert service.snapshot["enabled"]
    assert not service.commands
    service.updated.emit(service.snapshot)
    app.processEvents()


def test_single_toggle_and_snapshot_refresh_never_echo_commands():
    app = QApplication.instance() or QApplication([])
    service = FakeService()
    widget = WechatSettingsWidget(service)
    assert widget.enabled.isChecked()
    assert not hasattr(widget, "reminders")
    assert not hasattr(widget, "receipt_button")
    assert not hasattr(widget, "preview")
    service.updated.emit(service.snapshot)
    assert not service.commands
    widget.enabled.setChecked(False)
    assert service.commands == ["disable"]
    # The fake does not acknowledge; reflect actual state, not guessed success.
    assert widget.enabled.isChecked()
    service.snapshot.update(enabled=False, state="disabled", result="credential_delete_failed", deletion_failed=True)
    service.updated.emit(service.snapshot)
    assert not widget.enabled.isChecked()
    assert not widget.enabled.isEnabled()
    assert widget.bind_button.isHidden()
    assert widget.unbind_button.text() == "重试解绑"
    assert service.commands == ["disable"]
    widget.detach()


def test_compact_state_actions_and_results():
    app = QApplication.instance() or QApplication([])
    service = FakeService()
    widget = WechatSettingsWidget(service)
    assert "接口已接受，请核对手机" in widget.result.text()
    assert "2" in widget.pending.text()
    assert not widget.test_button.isHidden()
    assert widget.bind_button.isHidden()
    assert widget.confirm_button.isHidden()
    widget.test_button.click()
    assert service.commands == ["send_test"]
    service.snapshot.update(state="waiting_confirmation", candidate="demo", pending_count=0, result="")
    service.updated.emit(service.snapshot)
    assert not widget.confirm_button.isHidden()
    assert widget.test_button.isHidden()
    assert widget.pending.isHidden()
    assert widget.result.isHidden()
    widget.confirm_button.click()
    assert service.commands == ["send_test", "confirm_candidate"]
    widget.detach()


def test_unified_general_draft_test_and_close_guard(monkeypatch):
    from vlc_monitor.models import AppConfig
    from vlc_monitor.ui.settings_dialog import SettingsDialog
    app = QApplication.instance() or QApplication([])
    config = AppConfig()
    service = FakeService()
    calls = []
    def apply(values):
        calls.append(values)
        for key, value in values.items():
            setattr(config, key, value)
    dialog = SettingsDialog(config, service, apply_local=apply,
                            test_local=lambda: calls.append("immediate"),
                            delayed_test_local=lambda: calls.append("delayed"))
    dialog.sound.setChecked(not config.sound_enabled)
    assert dialog.has_draft()
    dialog.test_button.click()
    assert calls == ["immediate"]
    assert config.sound_enabled != dialog.sound.isChecked()
    dialog.test_timing.setCurrentIndex(1)
    dialog.test_button.click()
    assert calls[-1] == "delayed"
    monkeypatch.setattr(dialog, "_resolve_draft", lambda: False)
    dialog.tabs.setCurrentIndex(1)
    assert dialog.tabs.currentIndex() == 0
    dialog.reject()
    assert not dialog.wechat_page._closing
    dialog.apply_button.click()
    assert not dialog.has_draft()
    assert config.sound_enabled == dialog.sound.isChecked()
    monkeypatch.setattr(dialog, "_resolve_draft", lambda: True)
    dialog.reject()
    assert dialog.wechat_page._closing
    assert not service.commands


def test_unbind_confirmation_defaults_to_cancel(monkeypatch):
    from PySide6.QtWidgets import QMessageBox
    app = QApplication.instance() or QApplication([])
    service = FakeService()
    widget = WechatSettingsWidget(service)
    def cancel(box):
        assert box.defaultButton().text() == "取消"
        assert box.escapeButton().text() == "取消"
        assert any(button.text() == "解绑" for button in box.buttons())
        return 0
    monkeypatch.setattr(QMessageBox, "exec", cancel)
    widget._unbind()
    assert not service.commands
    widget.detach()
