"""Single shared-frame preview with normalized ROI overlays and editing."""

from PySide6.QtCore import Qt, QRectF, Signal
from PySide6.QtGui import QColor, QImage, QPainter, QPen
from PySide6.QtWidgets import QWidget

from vlc_monitor.models import RegionConfig
from vlc_monitor.playback.frame_buffer import FrameSample
from vlc_monitor.ui.geometry import (
    content_rect, move_rect, normalized_from_points, resize_rect,
    to_display, to_normalized, valid_size,
)


class VideoCanvas(QWidget):
    regionSelected = Signal(str)
    regionCreated = Signal(object)
    regionEdited = Signal(str, object)
    editStarted = Signal(str)
    editCancelled = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumSize(320, 180)
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self._frame = None
        self._image = None
        self._regions = []
        self._statuses = {}
        self._selected = ""
        self._drawing = False
        self._stale = False
        self._stale_message = "视频更新已过期"
        self._drag = None
        self._draft = None
        self._hint = ""

    def set_frame(self, frame: FrameSample | None):
        if (frame is not None and self._frame is not None and
                frame.session_id == self._frame.session_id and
                frame.frame_seq == self._frame.frame_seq and
                frame.display_aspect == self._frame.display_aspect):
            return
        if frame is None or (self._frame and (
            frame.session_id != self._frame.session_id or
            frame.width != self._frame.width or frame.height != self._frame.height or
            abs(frame.display_aspect - self._frame.display_aspect) > 0.001
        )):
            self._cancel_drag()
        # QImage borrows immutable BGRA storage; keep the sample alive for its lifetime.
        self._image = None
        self._frame = frame
        if frame is not None:
            self._image = QImage(frame.pixels.data, frame.width, frame.height,
                                 frame.stride, QImage.Format.Format_RGB32)
        self.update()

    def set_regions(self, regions: list[RegionConfig]):
        self._regions = list(regions)
        if self._drag and self._drag[0] != "draw" and not any(r.id == self._drag[1] for r in regions):
            self._cancel_drag()
        self.update()

    def set_statuses(self, statuses: dict):
        self._statuses = dict(statuses)
        self.update()

    def set_selected(self, region_id: str):
        self._selected = region_id
        self.update()

    def set_drawing(self, enabled: bool):
        self._cancel_drag()
        self._drawing = enabled
        self.setCursor(Qt.CursorShape.CrossCursor if enabled else Qt.CursorShape.ArrowCursor)
        self._hint = "拖动框选，Esc 取消；至少 16 × 16 视频像素" if enabled else ""
        if enabled:
            self.setFocus()
        self.update()

    def keyPressEvent(self, event):
        if event.key() == Qt.Key.Key_Escape and (self._drawing or self._drag):
            self.set_drawing(False)
            event.accept()
        else:
            super().keyPressEvent(event)

    def set_stale(self, stale: bool, message="视频更新已过期"):
        self._stale = stale
        self._stale_message = message
        if stale:
            self._cancel_drag()
        self.update()

    def _content(self):
        return content_rect(self.width(), self.height(), self._frame.display_aspect) if self._frame else (0, 0, 0, 0)

    def _cancel_drag(self, notify=True):
        drag = self._drag
        self._drag = None
        self._draft = None
        if notify and drag and drag[0] != "draw" and drag[4]:
            self.editCancelled.emit(drag[1])

    def _status(self, region):
        if not region.enabled:
            return "已停用", "paused"
        if self._stale:
            return "不可判定", "unknown"
        value = self._statuses.get(region.id, "待检测")
        if isinstance(value, dict):
            if "black" in value or "freeze" in value:
                values = [str(value[k]) for k in ("black", "freeze") if k in value]
                text = " / ".join(f"{'黑屏' if k == 'black' else '静帧'} {value[k]}"
                                  for k in ("black", "freeze") if k in value)
                if all("不可判定" in v for v in values):
                    return text, "unknown"
                if any("告警" in v or "恢复确认" in v for v in values):
                    return text, "alarm"
                return text, "normal" if all(v == "正常" for v in values) else "warming"
            return str(value.get("text", value.get("state", "待检测"))), value.get("state", "unknown")
        text = str(value)
        if any(word in text for word in ("不可判定", "无效", "unknown", "invalid")):
            return text, "unknown"
        if any(word in text for word in ("告警", "黑屏", "静帧", "alarm", "black", "freeze")):
            return text, "alarm"
        return text, "normal" if text in ("正常", "normal") else "warming"

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor("#101720"))
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        if self._image is None:
            painter.setPen(QColor("#9aafc4"))
            painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, "暂无视频\n连接视频源后，点击“新增区域”框选监播范围")
            return
        content = self._content()
        painter.drawImage(QRectF(*content), self._image)
        painter.setClipRect(QRectF(*content))
        colors = {"alarm": "#ff605f", "unknown": "#a0adbd", "warming": "#e4b864", "paused": "#8290a0", "normal": "#51d8bb"}
        for region in self._regions:
            rect = self._draft if self._drag and self._drag[1] == region.id else region.rect
            box = QRectF(*to_display(rect, content))
            text, state = self._status(region)
            color = QColor(colors.get(state, "#e4b864"))
            selected = region.id == self._selected
            painter.setPen(QPen(color, 3 if selected else 2,
                                Qt.PenStyle.DashLine if state in ("unknown", "paused") else Qt.PenStyle.SolidLine))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRect(box)
            label = f"{region.name} · {text}"
            metrics = painter.fontMetrics()
            label_width = min(metrics.horizontalAdvance(label) + 14, content[2])
            label_x = min(box.x(), content[0] + content[2] - label_width)
            label_y = max(content[1], box.y() - metrics.height() - 10)
            label_box = QRectF(label_x, label_y, label_width, metrics.height() + 8)
            painter.fillRect(label_box, QColor(15, 25, 38, 225))
            painter.setPen(color)
            painter.drawText(label_box.adjusted(6, 0, -4, 0), Qt.AlignmentFlag.AlignVCenter,
                             metrics.elidedText(label, Qt.TextElideMode.ElideRight, int(label_width - 10)))
            if selected:
                painter.fillRect(QRectF(box.right() - 5, box.bottom() - 5, 10, 10), color)
        if self._drag and self._drag[0] == "draw" and self._draft:
            painter.setPen(QPen(QColor("#78b9ff"), 2, Qt.PenStyle.DashLine))
            painter.drawRect(QRectF(*to_display(self._draft, content)))
        painter.setClipping(False)
        if self._stale:
            painter.fillRect(self.rect(), QColor(8, 16, 26, 105))
            painter.setPen(QColor("#ffd28a"))
            painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, self._stale_message + " · 检测暂停")
        elif self._hint:
            painter.fillRect(QRectF(0, self.height() - 30, self.width(), 30), QColor(15, 25, 38, 230))
            painter.setPen(QColor("#cad9e8"))
            painter.drawText(self.rect().adjusted(12, 0, -12, -6), Qt.AlignmentFlag.AlignBottom, self._hint)

    def mousePressEvent(self, event):
        if event.button() != Qt.MouseButton.LeftButton or not self._frame or self._stale:
            return
        point = to_normalized((event.position().x(), event.position().y()), self._content())
        if point is None:
            return
        self._hint = ""
        if self._drawing:
            self._drag = ("draw", "", point, None, False)
            self._draft = (*point, 0, 0)
        else:
            for region in sorted(self._regions, key=lambda r: r.id == self._selected, reverse=True):
                box = QRectF(*to_display(region.rect, self._content()))
                handle = QRectF(box.right() - 8, box.bottom() - 8, 16, 16)
                if box.contains(event.position()) or (region.id == self._selected and handle.contains(event.position())):
                    mode = "resize" if region.id == self._selected and handle.contains(event.position()) else "move"
                    self._selected = region.id
                    self._drag = (mode, region.id, point, region.rect, False)
                    self._draft = region.rect
                    self.regionSelected.emit(region.id)
                    break
        self.update()

    def mouseMoveEvent(self, event):
        if not self._drag or not self._frame:
            return
        point = to_normalized((event.position().x(), event.position().y()), self._content(), clamp=True)
        mode, region_id, start, original, started = self._drag
        if point == start and not started:
            return
        if mode != "draw" and not started:
            self._drag = (mode, region_id, start, original, True)
            self.editStarted.emit(region_id)
            if self._drag is None:
                return
        if mode == "draw":
            self._draft = normalized_from_points(start, point)
        elif mode == "move":
            self._draft = move_rect(original, point[0] - start[0], point[1] - start[1])
        else:
            self._draft = resize_rect(original, point, self._frame.width, self._frame.height)
        self.update()

    def mouseReleaseEvent(self, event):
        if event.button() != Qt.MouseButton.LeftButton or not self._drag:
            return
        self.mouseMoveEvent(event)
        if not self._drag or not self._frame:
            return
        mode, region_id, _, _, started = self._drag
        rect = self._draft
        self._cancel_drag(notify=False)
        if rect and valid_size(rect, self._frame.width, self._frame.height):
            if mode == "draw":
                self.regionCreated.emit(rect)
            elif started:
                self.regionEdited.emit(region_id, rect)
        else:
            self._hint = "区域过小：请至少框选 16 × 16 视频像素"
            if mode != "draw" and started:
                self.editCancelled.emit(region_id)
        self.update()
