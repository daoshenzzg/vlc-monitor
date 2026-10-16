"""A disposable view over the application-owned WeChat notification service."""
from __future__ import annotations

from PySide6.QtCore import QSignalBlocker, Qt, Slot
from PySide6.QtGui import QColor, QImage, QPixmap
from PySide6.QtWidgets import QCheckBox, QDialog, QHBoxLayout, QLabel, QMessageBox, QPushButton, QVBoxLayout, QWidget


STATES = {
    "disabled": "关闭（不联网）", "unbound": "待绑定", "waiting_scan": "等待本人扫码",
    "waiting_challenge": "等待本人发送一次性挑战", "waiting_confirmation": "等待本人确认候选",
    "context_unverified": "已绑定 · 上下文待验证", "trial_sendable": "已绑定 · 通知已开启",
    "session_invalid": "会话或发送上下文失效，请解绑后重新确认", "expired": "绑定已过期，请重新开始",
    "credential_unavailable": "系统凭证保护不可用；拒绝明文降级", "blocked": "登录分支尚未验证，已停止",
    "failed": "本次绑定失败，已停止",
}
RESULTS = {
    "test_pending": "正在请求发送本次测试；接口结果和手机收件尚未确认。",
    "credential_read_failed": "系统凭证读取失败。未启用通知。",
    "credential_write_failed": "系统凭证写入或回读失败；未进入可发送状态。请解绑清理。",
    "credential_delete_failed": "解绑未彻底完成：系统凭证删除失败。通道保持禁用，请重试解绑。",
    "binding_saved": "绑定已保存，可发送测试。",
    "test_api_accepted": "接口已接受，请核对手机。",
    "phone_receipt_confirmed_by_user": "已记录手机收件确认。",
    "unbound": "已解绑并验证本地系统凭证删除；已在途请求不能撤回。",
    "binding_expired": "二维码或挑战超时，已清除本次绑定材料。",
    "unsupported_login_branch": "遇到额外配对码、跨机房重定向或旧绑定分支，尚未验证，已安全停止。",
    "redirect_rejected": "服务端重定向未获允许，已拒绝。",
    "session_invalid": "会话或上下文失效；不自动重登、不自动保活。",
    "network_failed": "网络请求失败，未自动重试。",
    "request_failed": "请求未能完成。",
    "uncertain": "发送结果不确定；重新测试可能重复，请先查看手机。",
    "api_rejected": "接口拒绝本次操作。",
    "invalid_response": "服务端响应不符合已审计契约，已停止。",
    "alarm_api_accepted": "接口已接受，请核对手机。",
    "alarm_uncertain": "本次告警/恢复发送结果不确定；再次发送可能重复，请核对手机。",
    "alarm_failed": "本次告警/恢复发送失败；请检查会话状态并保留其他值守渠道。",
    "alarm_transient": "临时网络或限流故障；正在按有界策略重试，不代表手机已收件。",
    "alarm_session_invalid": "会话或发送上下文已失效，通知已暂停；请解绑后重新绑定。",
    "queue_full": "待发送队列已满，本次通知未入队。",
    "expired": "待发送通知已过期，未继续发送。",
    "cancelled": "本次待发送通知已取消；已在途请求不能撤回。",
    "transient": "本次请求遇到临时故障；请核对后续结果，不代表手机已收件。",
}


class WechatSettingsWidget(QWidget):
    """Disposable subscriber; the application retains ownership of the service."""

    def __init__(self, service, parent=None):
        super().__init__(parent)
        self.service = service
        self._closing = False
        layout = QVBoxLayout(self)
        self.enabled = QCheckBox("接收微信通知")
        layout.addWidget(self.enabled)
        self.status = QLabel()
        self.result = QLabel()
        self.pending = QLabel()
        self.qr = QLabel()
        self.qr.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.challenge = QLabel()
        self.challenge.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        for label in (self.status, self.result, self.pending, self.challenge):
            label.setTextFormat(Qt.TextFormat.PlainText)
            label.setWordWrap(True)
        for label in (self.status, self.result, self.pending, self.qr, self.challenge):
            layout.addWidget(label)
        self.bind_button = QPushButton("绑定微信")
        self.confirm_button = QPushButton("确认是本人发送")
        self.test_button = QPushButton("发送测试")
        self.unbind_button = QPushButton("解绑")
        for button in (self.bind_button, self.confirm_button, self.test_button, self.unbind_button):
            button.setAutoDefault(False)
        actions = QHBoxLayout()
        for button in (self.bind_button, self.confirm_button, self.test_button):
            actions.addWidget(button)
        actions.addStretch()
        actions.addWidget(self.unbind_button)
        layout.addLayout(actions)
        layout.addStretch()
        note = QLabel("关闭设置不改变通知状态。开关更改需在主窗口保存配置。")
        note.setWordWrap(True)
        layout.addWidget(note)
        self.enabled.toggled.connect(self._toggle_enabled)
        self.bind_button.clicked.connect(self._bind)
        self.confirm_button.clicked.connect(lambda: self.service.command("confirm_candidate"))
        self.test_button.clicked.connect(lambda: self.service.command("send_test"))
        self.unbind_button.clicked.connect(self._unbind)
        self.service.updated.connect(self._update)
        self._update(self.service.snapshot)

    def _toggle_enabled(self, enabled):
        self.service.command("enable" if enabled else "disable")
        # Reflect acknowledged runtime state, including failed enable attempts.
        self._update(self.service.snapshot)

    def _bind(self):
        try:
            import qrcode
        except ImportError:
            QMessageBox.warning(self, "缺少二维码组件", "当前构建缺少二维码组件，未发起网络请求。")
            return
        box = QMessageBox(QMessageBox.Icon.Question, "绑定微信",
                          "请仅用本人微信扫码，并亲自发送一次性挑战。不要转发二维码或挑战。", parent=self)
        start = box.addButton("开始绑定", QMessageBox.ButtonRole.AcceptRole)
        cancel = box.addButton("取消", QMessageBox.ButtonRole.RejectRole)
        box.setDefaultButton(cancel)
        box.setEscapeButton(cancel)
        box.exec()
        if box.clickedButton() is start:
            self.service.command("start_binding")

    def _unbind(self):
        box = QMessageBox(QMessageBox.Icon.Warning, "解绑微信",
                          "关闭通知并删除本机系统凭证？已经在途的请求无法撤回。", parent=self)
        unbind = box.addButton("解绑", QMessageBox.ButtonRole.DestructiveRole)
        cancel = box.addButton("取消", QMessageBox.ButtonRole.RejectRole)
        box.setDefaultButton(cancel)
        box.setEscapeButton(cancel)
        box.exec()
        if box.clickedButton() is unbind:
            self.service.command("unbind")

    @Slot(object)
    def _update(self, value):
        if self._closing:
            return
        state, busy = value["state"], value["busy"]
        enabled = value["enabled"]
        deletion_failed = value.get("deletion_failed", False)
        with QSignalBlocker(self.enabled):
            self.enabled.setChecked(enabled)
        self.enabled.setEnabled(not deletion_failed)
        self.status.setText("解绑未完成，请重试解绑" if deletion_failed else STATES.get(state, "暂不可用"))
        count = value.get("pending_count", 0)
        self.pending.setText(f"待发送：{count} 条" if count else "")
        self.pending.setVisible(bool(count))
        result = value.get("result", "")
        if value.get("delivery_result"):
            result = {"accepted": "alarm_api_accepted", "failed": "alarm_failed", "uncertain": "alarm_uncertain"}.get(value["delivery_result"], value["delivery_result"])
        self.result.setText("最近结果：" + RESULTS.get(result, "操作未完成，请检查通知状态。") if result else "")
        self.result.setVisible(bool(result))
        bind = enabled and not deletion_failed and state in ("unbound", "expired", "blocked", "failed")
        confirm = enabled and state == "waiting_confirmation"
        sendable = enabled and state in ("context_unverified", "trial_sendable")
        self.bind_button.setVisible(bind)
        self.bind_button.setEnabled(not busy)
        self.confirm_button.setVisible(confirm)
        self.confirm_button.setEnabled(not busy)
        self.test_button.setVisible(sendable)
        self.test_button.setText("发送中" if busy and value.get("result") == "test_pending" else "发送测试")
        self.test_button.setEnabled(not busy)
        # A disabled service intentionally cannot inspect the credential store;
        # keep explicit cleanup available without guessing whether a binding exists.
        self.unbind_button.setVisible(deletion_failed or not enabled or state in ("context_unverified", "trial_sendable", "session_invalid", "credential_unavailable"))
        self.unbind_button.setText("重试解绑" if deletion_failed else "解绑")
        self.unbind_button.setEnabled(not busy)
        challenge, candidate = value.get("challenge", ""), value.get("candidate", "")
        self.challenge.setText("请在本人微信会话发送（3 分钟内有效）：\n" + challenge if challenge else
                               "挑战已匹配。仅当刚刚由本人亲自发送时确认。" if candidate else "")
        self.challenge.setVisible(bool(challenge or candidate))
        content = value.get("qr_content", "")
        self.qr.setVisible(bool(content))
        if content:
            import qrcode
            matrix = qrcode.QRCode(border=4, box_size=1)
            matrix.add_data(content)
            matrix.make(fit=True)
            pixels = matrix.get_matrix()
            image = QImage(len(pixels), len(pixels), QImage.Format.Format_RGB32)
            for y, line in enumerate(pixels):
                for x, dark in enumerate(line):
                    image.setPixelColor(x, y, QColor("black" if dark else "white"))
            self.qr.setPixmap(QPixmap.fromImage(image).scaled(240, 240, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.FastTransformation))
        else:
            self.qr.clear()

    def detach(self):
        if not self._closing:
            self._closing = True
            self.service.updated.disconnect(self._update)
            self.qr.clear()
            self.challenge.clear()

    def closeEvent(self, event):
        self.detach()
        super().closeEvent(event)


class WechatSettingsDialog(QDialog):
    """Compatibility wrapper for callers not yet using the unified settings."""

    def __init__(self, service, parent=None):
        super().__init__(parent)
        self.setWindowTitle("微信通知")
        self.resize(560, 420)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
        layout = QVBoxLayout(self)
        self.page = WechatSettingsWidget(service, self)
        layout.addWidget(self.page)
        close = QPushButton("关闭")
        close.clicked.connect(self.reject)
        layout.addWidget(close)

    @property
    def _closing(self):
        return self.page._closing

    def done(self, result):
        self.page.detach()
        super().done(result)

    def closeEvent(self, event):
        self.page.detach()
        super().closeEvent(event)
