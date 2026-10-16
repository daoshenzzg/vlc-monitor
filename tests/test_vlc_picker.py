from pathlib import Path
from types import SimpleNamespace

import pytest
from PySide6.QtWidgets import QApplication, QDialog

from vlc_monitor.platform.vlc_locator import VlcError
from vlc_monitor.ui import vlc_picker


@pytest.fixture(scope="module")
def app():
    instance = QApplication.instance() or QApplication([])
    yield instance


@pytest.mark.parametrize("text, expected", [
    ("", ""), ("   ", ""),
    ('  "/Applications/My VLC.app"  ', "/Applications/My VLC.app"),
    ("'~/Applications/My VLC.app'", str(Path.home() / "Applications/My VLC.app")),
    ("C:/Program Files/VideoLAN/VLC", "C:/Program Files/VideoLAN/VLC"),
    ("/Applications/VLC's copy.app", "/Applications/VLC's copy.app"),
])
def test_normalize(text, expected):
    assert vlc_picker.normalize_vlc_path(text) == expected


def test_unknown_home_name_remains_for_validation(monkeypatch):
    def unknown(_):
        raise RuntimeError("Could not determine home directory")
    monkeypatch.setattr(vlc_picker.Path, "expanduser", unknown)
    assert vlc_picker.normalize_vlc_path("~missing/VLC.app") == "~missing/VLC.app"


def test_validate_explicit_path_before_accepting(app, monkeypatch):
    received = []
    monkeypatch.setattr(vlc_picker, "locate", lambda path: received.append(path) or SimpleNamespace(root=Path("/valid/VLC.app")))
    dialog = vlc_picker._VlcPicker(None, ' "/valid/VLC.app" ')
    dialog.accept()
    assert received == ["/valid/VLC.app"]
    assert dialog.result() == QDialog.DialogCode.Accepted
    assert dialog.selected_path == "/valid/VLC.app"


@pytest.mark.parametrize("code", ["invalid_path", "architecture", "plugins"])
def test_invalid_selection_does_not_accept_or_change_initial(app, monkeypatch, code):
    def fail(path):
        assert path == "/bad/VLC.app"
        raise VlcError(code, "校验失败")
    monkeypatch.setattr(vlc_picker, "locate", fail)
    dialog = vlc_picker._VlcPicker(None, "/bad/VLC.app")
    dialog.accept()
    assert dialog.result() != QDialog.DialogCode.Accepted
    assert dialog.selected_path is None
    assert dialog.path.text() == "/bad/VLC.app"
    assert code in dialog.error.text()


def test_empty_path_never_auto_detects(app, monkeypatch):
    monkeypatch.setattr(vlc_picker, "locate", lambda _: pytest.fail("must not auto-detect"))
    dialog = vlc_picker._VlcPicker(None, "  ")
    dialog.accept()
    assert dialog.selected_path is None
    assert dialog.error.text()


@pytest.mark.parametrize("platform", ["darwin", "win32"])
def test_platform_browse_and_browse_cancel_preserve_input(app, monkeypatch, platform):
    monkeypatch.setattr(vlc_picker.sys, "platform", platform)
    calls = []
    if platform == "darwin":
        def browse(*args):
            calls.append(args)
            return "", ""
        monkeypatch.setattr(vlc_picker.QFileDialog, "getOpenFileName", browse)
        monkeypatch.setattr(vlc_picker.QFileDialog, "getExistingDirectory", lambda *a: pytest.fail("Mac requires package/file mode"))
    else:
        def browse(*args):
            calls.append(args)
            return ""
        monkeypatch.setattr(vlc_picker.QFileDialog, "getExistingDirectory", browse)
    dialog = vlc_picker._VlcPicker(None, "/original/VLC.app")
    dialog._browse()
    assert len(calls) == 1
    assert dialog.path.text() == "/original/VLC.app"
    assert dialog.selected_path is None


def test_public_cancel_returns_none(app, monkeypatch):
    monkeypatch.setattr(vlc_picker._VlcPicker, "exec", lambda self: QDialog.DialogCode.Rejected)
    monkeypatch.setattr(vlc_picker, "locate", lambda _: pytest.fail("cancel must not validate or load"))
    assert vlc_picker.choose_vlc_path(initial="/original/VLC.app") is None


def test_startup_cancel_is_explicit_exit(app):
    from PySide6.QtWidgets import QDialogButtonBox
    dialog = vlc_picker._VlcPicker(None, "", startup=True)
    buttons = dialog.findChild(QDialogButtonBox)
    assert buttons.button(QDialogButtonBox.StandardButton.Cancel).text() == "退出"


def test_windows_backslash_path_is_not_rewritten():
    assert vlc_picker.normalize_vlc_path('  "C:\\Program Files\\VideoLAN\\VLC" ') == 'C:\\Program Files\\VideoLAN\\VLC'
