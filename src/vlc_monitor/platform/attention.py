"""Best-effort Qt attention requests, not a guarantee of OS focus transfer."""


def request_attention(window, foreground=False):
    from PySide6.QtCore import QThread, Qt
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance()
    if app is None or QThread.currentThread() != app.thread():
        raise RuntimeError("Window attention must run on the Qt application thread")
    if foreground:
        if window.isMinimized():
            # Retain WindowMaximized when a maximized window was minimized.
            window.setWindowState(window.windowState() & ~Qt.WindowState.WindowMinimized)
        window.raise_()
        window.activateWindow()
    QApplication.alert(window, 0)
