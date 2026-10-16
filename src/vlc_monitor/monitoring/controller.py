"""One worker owns playback, authoritative state, and detection commands."""

from dataclasses import replace
from copy import deepcopy
import time

from PySide6.QtCore import QObject, QTimer, Signal, Slot

from vlc_monitor.detection import MonitorEngine
from vlc_monitor.playback.frame_buffer import LatestFrame
from vlc_monitor.playback.vlc_backend import VlcBackend
from vlc_monitor.platform.vlc_locator import VlcError


class MonitorWorker(QObject):
    status = Signal(object)
    events = Signal(object)
    command_result = Signal(object)
    error = Signal(str)
    finished = Signal()
    shutdown_failed = Signal(str)

    def __init__(self, location, config):
        super().__init__()
        self.location = location
        self.config = deepcopy(config)
        self.regions = deepcopy(config.regions)
        self.engine = MonitorEngine(config)
        self.frames = LatestFrame()
        self.valid_frames = LatestFrame()
        self.backend = None
        self.timer = None
        self.opened_at = 0.0
        self.last_sampled = 0.0
        self.last_status = 0.0
        self.monitor_aspect = None
        self.paused_revisions = {}
        self.problem = "未连接"
        self.playback_state = "disconnected"
        self.state_version = 0
        self.run_id = 0
        self.open_request = None
        self.open_cancel = None
        self.backend_request = None
        self.cleanup_failed = False
        self._shutting_down = False
        self._completed = set()
        self.command_durations = {}

    @Slot()
    def initialize(self):
        self.timer = QTimer(self)
        self.timer.setInterval(50)
        self.timer.timeout.connect(self.poll)
        self.timer.start()
        self.publish_status()

    def can_monitor(self):
        if self.paused_revisions:
            return False, "请先应用或取消区域编辑。"
        now = time.monotonic()
        frame = self.valid_frames.latest()
        if self.playback_state != "playing" or not self.backend or not frame or now - frame.received_at >= self.config.update_timeout:
            return False, "无新鲜有效视频，请先连接并等待新画面。"
        if not any(r.enabled and (r.black_enabled or r.freeze_enabled) for r in self.regions):
            return False, "请至少启用一个区域及其检测规则。"
        if self.backend.metadata_error:
            return False, self.backend.metadata_error
        for region in self.regions:
            if region.enabled and (region.rect[2] * frame.width < 16 or region.rect[3] * frame.height < 16):
                return False, f"区域“{region.name}”小于 16×16 视频像素，请重新框选。"
        return True, ""

    def publish_status(self, request_id=None):
        self.state_version += 1
        now = time.monotonic()
        frame = self.valid_frames.latest()
        allowed, reason = self.can_monitor()
        self.status.emit({"state_version": self.state_version, "request_id": request_id,
            "playback_state": self.playback_state, "monitoring_running": self.engine.running,
            "running": self.engine.running, "connected": self.playback_state == "playing",
            "session": self.backend.session_id if self.backend else "", "run_id": self.run_id,
            "can_monitor": allowed, "cannot_monitor_reason": reason,
            "statuses": deepcopy(self.engine.statuses), "stream": self.engine.stream_status,
            "problem": self.problem, "age": max(0, now - frame.received_at) if frame else None,
            "seq": frame.frame_seq if frame else 0,
            "media_state": "CleanupFailed" if self.cleanup_failed else self.backend.status() if self.backend else "Stopped"})
        self.last_status = now

    def emit_events(self, events):
        if events:
            # The GUI must know the new session/run before its first event.
            self.publish_status()
            self.events.emit([replace(event, run_id=self.run_id) for event in events])

    def complete(self, request_id, action, success, reason=""):
        if request_id:
            self._completed.add(request_id)
            if len(self._completed) > 1024:
                self._completed = {request_id}
        self.publish_status(request_id)
        self.command_result.emit({"request_id": request_id, "action": action,
            "success": success, "reason": reason, "state_version": self.state_version})

    @Slot(object)
    def execute(self, command):
        try:
            self._execute(command)
        except Exception:
            request_id = self.open_request
            self.open_request = None
            self.open_cancel = None
            if request_id and request_id != command["request_id"]:
                self.complete(request_id, "connect", False, "播放器清理失败，尚未安全释放；请重试停止。")
            self.complete(command["request_id"], command["action"], False,
                          "操作失败；播放器若尚未安全释放，请重试停止，不会另建播放器。")

    def _execute(self, command):
        request_id, action = command["request_id"], command["action"]
        if request_id in self._completed or self._shutting_down:
            return
        if action == "connect":
            if self.cleanup_failed:
                return self.complete(request_id, action, False, "旧播放器尚未安全释放，请先重试停止播放。")
            if self.open_request:
                return self.complete(request_id, action, False, "已有连接正在等待完成。")
            self.connect_source(command["source"], request_id, command.get("cancel"))
        elif action == "cancel":
            target = command.get("target")
            if self.open_request == target:
                self._finish_open(False, "连接已取消", disconnect=True)
            elif target and self.backend_request == target:
                # First-frame success can be queued to the GUI when Cancel is
                # clicked; it still cancels that exact completed connection.
                self.disconnect_source("连接已取消")
            self.complete(request_id, action, not self.cleanup_failed,
                          "播放器未安全释放，请重试停止。" if self.cleanup_failed else "")
        elif action == "disconnect":
            if self.open_request:
                self._finish_open(False, "连接已停止", disconnect=True)
            self.playback_state = "stopping"
            self.publish_status(request_id)
            self.disconnect_source("手动停止播放")
            self.complete(request_id, action, True)
        elif action == "start":
            allowed, reason = self.can_monitor()
            if self.open_request or self.engine.running or not allowed:
                return self.complete(request_id, action, False, reason or "监播已经运行或连接未完成。")
            self.start_monitoring()
            self.complete(request_id, action, True)
        elif action == "stop":
            self.stop_monitoring()
            self.complete(request_id, action, True)
        else:
            self.complete(request_id, action, False, "未知操作")

    def connect_source(self, source, request_id=None, cancel=None):
        if self.open_request:
            return
        self.disconnect_source("会话结束")
        self.open_request = request_id
        self.open_cancel = cancel
        self.playback_state = "opening"
        self.problem = "正在打开媒体…"
        self.publish_status(request_id)
        started = time.monotonic()
        try:
            # libVLC initialization is synchronous. Cancellation is checked at
            # its first safe return point; never kill a thread with callbacks.
            self.backend = VlcBackend(self.location)
            self.backend_request = request_id
            if cancel and cancel.is_set():
                return self._finish_open(False, "连接已取消", disconnect=True)
            self.backend.play(source)
            if cancel and cancel.is_set():
                return self._finish_open(False, "连接已取消", disconnect=True)
            self.opened_at = time.monotonic()
            self.playback_state = "waiting"
            self.problem = "等待首个有效视频帧…"
            self.publish_status(request_id)
        except Exception as exc:
            reason = str(exc) if isinstance(exc, VlcError) else "播放初始化失败，请检查本机 VLC。"
            self._finish_open(False, reason, disconnect=True)
        finally:
            self.command_durations["open_sync_seconds"] = round(time.monotonic() - started, 4)

    def _finish_open(self, success, reason="", disconnect=False):
        request_id = self.open_request
        self.open_request = None
        self.open_cancel = None
        if disconnect:
            try:
                self.disconnect_source("会话结束")
            except Exception:
                self.complete(request_id, "connect", False, "连接失败且播放器未安全释放；请重试停止播放。")
                return
        self.playback_state = "playing" if success else "disconnected" if "取消" in reason or "停止" in reason else "error"
        self.problem = reason
        self.complete(request_id, "connect", success, reason)

    @Slot()
    def disconnect(self):
        self.disconnect_source("手动停止播放")

    def disconnect_source(self, reason):
        ended = self.engine.stop(time.monotonic(), reason)
        if self.backend:
            try:
                self.backend.close()
            except Exception:
                self.cleanup_failed = True
                self.playback_state = "cleanup_error"
                self.problem = "播放器尚未安全释放，请重试停止；禁止新建播放会话。"
                self.emit_events(ended)
                raise
            self.backend = None
        self.cleanup_failed = False
        self.frames.clear()
        self.valid_frames.clear()
        self.backend_request = None
        self.monitor_aspect = None
        self.playback_state = "disconnected"
        self.problem = "未连接"
        self.emit_events(ended)

    @Slot()
    def start_monitoring(self):
        allowed, reason = self.can_monitor()
        if not allowed:
            self.error.emit(reason)
            return
        frame = self.valid_frames.latest()
        self.monitor_aspect = frame.display_aspect
        self.run_id += 1
        events = self.engine.start(frame.session_id, self.regions, time.monotonic())
        self.publish_status()
        self.emit_events(events)
        self.last_sampled = 0.0

    @Slot()
    def stop_monitoring(self):
        self.emit_events(self.engine.stop(time.monotonic(), "手动停止"))
        self.publish_status()

    @Slot(object)
    def update_regions(self, regions):
        self.regions = deepcopy(regions)
        by_id = {r.id: r for r in regions}
        self.paused_revisions = {rid: revision for rid, revision in self.paused_revisions.items() if rid in by_id and by_id[rid].revision == revision}
        projected = [replace(r, enabled=False, revision=r.revision + 1) if r.id in self.paused_revisions else r for r in regions]
        self.emit_events(self.engine.set_regions(projected, time.monotonic(), "区域删除/停用/规则修改"))
        self.publish_status()

    @Slot(str)
    def pause_region(self, region_id):
        region = next((r for r in self.regions if r.id == region_id), None)
        if region:
            self.paused_revisions[region_id] = region.revision
        paused = [replace(r, enabled=False, revision=r.revision + 1) if r.id in self.paused_revisions else r for r in self.regions]
        self.emit_events(self.engine.set_regions(paused, time.monotonic(), "区域编辑"))
        self.publish_status()

    @Slot()
    def poll(self):
        if self._shutting_down:
            return
        if self.cleanup_failed:
            if time.monotonic() - self.last_status >= 0.5:
                self.publish_status()
            return
        frame = None
        if self.backend:
            if self.open_cancel and self.open_cancel.is_set():
                return self._finish_open(False, "连接已取消", disconnect=True)
            frame = self.backend.poll_frame()
            preview = self.backend.preview_frames.latest()
            now = time.monotonic()
            if preview:
                self.frames.publish(preview)
            if frame:
                self.valid_frames.publish(frame)
                self.problem = ""
                self.playback_state = "playing"
                if self.open_request and self.backend.status() != "Error":
                    self._finish_open(True)
                if self.engine.running and self.monitor_aspect and abs(frame.display_aspect / self.monitor_aspect - 1) > 0.01:
                    self.emit_events(self.engine.stop(now, "视频宽高比改变"))
                    self.error.emit("视频显示比例已改变，监播已暂停；请复核区域后重新开始。")
                    self.monitor_aspect = None
            elif self.playback_state == "waiting" and now - self.opened_at > self.config.first_frame_timeout:
                self._finish_open(False, "等待视频超时，请检查地址、视频轨或手动重连。", disconnect=True)
                return
            if self.backend and self.backend.status() == "Error":
                self.playback_state = "error"
                self.problem = "媒体打开或解码失败，请检查地址及本机 VLC 支持能力。"
                if self.open_request:
                    self._finish_open(False, self.problem, disconnect=True)
                    return
                # Playback error and ongoing stream detection are orthogonal.
        else:
            now = time.monotonic()
        if self.engine.running:
            if frame:
                self.emit_events(self.engine.observe(frame, now))
            self.emit_events(self.engine.tick(now))
            if frame and now - self.last_sampled >= self.config.detection_interval:
                self.emit_events(self.engine.consume(frame, now))
                self.last_sampled = now
        if now - self.last_status >= 0.5:
            if self.backend:
                self.backend.refresh_metadata()
            self.publish_status()

    @Slot()
    def shutdown(self):
        self._shutting_down = True
        if self.timer:
            self.timer.stop()
        try:
            if self.open_request:
                self._finish_open(False, "连接已停止", disconnect=True)
            self.disconnect_source("会话结束")
        except Exception:
            self._shutting_down = False
            self.publish_status()
            if self.timer:
                self.timer.start()
            self.shutdown_failed.emit("播放器清理失败，仍保留回调及内存所有者；请重试停止/退出。不强杀线程。")
        else:
            self.finished.emit()
