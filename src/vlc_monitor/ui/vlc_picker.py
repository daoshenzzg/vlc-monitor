"""Shared, non-mutating VLC location selection and validation."""

from pathlib import Path
import sys

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog, QDialogButtonBox, QFileDialog, QHBoxLayout, QLabel,
    QLineEdit, QPushButton, QVBoxLayout, QWidget,
)

from vlc_monitor.platform.vlc_locator import VlcError, locate


def normalize_vlc_path(text: str) -> str:
    value = text.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
        value = value[1:-1]
    if not value:
        return ""
    try:
        return str(Path(value).expanduser())
    except RuntimeError:
        # Unknown ~user input should reach normal path validation, not crash UI.
        return value


class _VlcPicker(QDialog):
    def __init__(self, parent: QWidget | None, initial: str, *, startup: bool = False):
        super().__init__(parent)
        self.selected_path: str | None = None
        self.mac = sys.platform == "darwin"
        self.setWindowTitle("选择 VLC 应用" if self.mac else "选择 VLC 安装目录")
        self.setMinimumWidth(520)
        layout = QVBoxLayout(self)
        hint = QLabel(
            "选择完整 VLC.app 应用包，或粘贴应用完整路径。"
            if self.mac else "选择 VLC 安装目录，或粘贴完整目录路径。"
        )
        hint.setWordWrap(True)
        layout.addWidget(hint)
        row = QHBoxLayout()
        self.path = QLineEdit(initial)
        self.path.setPlaceholderText("/Applications/VLC.app" if self.mac else "C:/Program Files/VideoLAN/VLC")
        self.path.setAccessibleName("VLC 路径")
        row.addWidget(self.path)
        browse = QPushButton("浏览…")
        browse.clicked.connect(self._browse)
        row.addWidget(browse)
        layout.addLayout(row)
        self.error = QLabel()
        self.error.setWordWrap(True)
        self.error.setTextFormat(Qt.TextFormat.PlainText)
        layout.addWidget(self.error)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("确认路径")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("退出" if startup else "取消")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _browse(self):
        initial = normalize_vlc_path(self.path.text())
        if self.mac:
            # Use native file mode for package selection rather than directory
            # navigation, which forces users inside application bundles.
            path, _ = QFileDialog.getOpenFileName(
                self, "选择 VLC 应用", initial or "/Applications", "应用程序 (*.app)",
            )
        else:
            path = QFileDialog.getExistingDirectory(self, "选择 VLC 安装目录", initial)
        if path:
            self.path.setText(path)
            self.error.clear()

    def accept(self):
        path = normalize_vlc_path(self.path.text())
        if not path:
            self.error.setText("请选择或粘贴完整 VLC 路径。")
            return
        try:
            location = locate(path)
        except VlcError as exc:
            self.error.setText(f"{exc}（{exc.code}）")
            return
        except OSError as exc:
            self.error.setText(f"无法读取 VLC 路径：{exc}")
            return
        self.selected_path = str(location.root)
        super().accept()


def choose_vlc_path(parent: QWidget | None = None, initial: str = "", *, startup: bool = False) -> str | None:
    """Return a validated explicit location, or None without changing settings."""
    dialog = _VlcPicker(parent, initial, startup=startup)
    try:
        if dialog.exec() == QDialog.DialogCode.Accepted:
            return dialog.selected_path
        return None
    finally:
        dialog.deleteLater()
