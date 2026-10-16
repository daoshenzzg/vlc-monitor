"""Exactly one player; software output is shared by every consumer."""

import ctypes
import threading
import time
import uuid
from dataclasses import replace
import numpy as np

from vlc_monitor.platform.vlc_locator import load_binding, VlcError
from vlc_monitor.playback.frame_buffer import FrameSample, LatestFrame


class VlcBackend:
    def __init__(self, location):
        self.vlc = load_binding(location)
        self.location = location
        self.frames = LatestFrame()
        self.preview_frames = LatestFrame()
        self.session_id = str(uuid.uuid4())
        self._lock = threading.Lock()
        self._active = False
        self._seq = 0
        self._decoded_count = 0
        self._closed = False
        self._player_stopped = False
        self._player_released = False
        self._media_released = False
        self._instance_released = False
        self._storage = None
        self._allocation = None
        self._shape = (0, 0)
        self.display_aspect = 0.0
        self.metadata_ready = False
        self.metadata_error = ""
        self.instance = self.vlc.Instance("--no-video-title-show", "--no-osd", "--no-snapshot-preview", "--quiet")
        if self.instance is None:
            raise VlcError("plugins", "VLC 初始化失败，请检查 VLC 插件和依赖库。")
        self._log_callback = self.vlc.CallbackDecorators.LogCb(lambda *args: None)
        self.instance.log_set(self._log_callback, None)
        self.player = self.instance.media_player_new()
        # python-vlc's c_char_p copies the writable chroma argument into bytes.
        format_type = ctypes.CFUNCTYPE(ctypes.c_uint, ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(ctypes.c_char), *([ctypes.POINTER(ctypes.c_uint)] * 4))
        self._raw_format = format_type(self._format)
        self._callbacks = (
            ctypes.cast(self._raw_format, self.vlc.CallbackDecorators.VideoFormatCb),
            self.vlc.CallbackDecorators.VideoCleanupCb(self._cleanup),
            self.vlc.CallbackDecorators.VideoLockCb(self._video_lock),
            self.vlc.CallbackDecorators.VideoUnlockCb(self._unlock),
            self.vlc.CallbackDecorators.VideoDisplayCb(self._display),
        )
        fmt, cleanup, lock, unlock, display = self._callbacks
        self.player.video_set_callbacks(lock, unlock, display, None)
        self.player.video_set_format_callbacks(fmt, cleanup)
        self.media = None

    def _format(self, opaque, chroma, width, height, pitches, lines):
        try:
            w, h = int(width[0]), int(height[0])
            if not (0 < w <= 8192 and 0 < h <= 8192):
                return 0
            size = w * h * 4
            allocation = (ctypes.c_ubyte * (size + 31))()
            address = (ctypes.addressof(allocation) + 31) & ~31
            storage = (ctypes.c_ubyte * size).from_address(address)
            ctypes.memmove(chroma, b"RV32", 4)
            pitches[0], lines[0] = w * 4, h
            with self._lock:
                self._allocation, self._storage, self._shape = allocation, storage, (h, w)
        except Exception:
            # ctypes callbacks must return an explicit failure, never unwind.
            return 0
        return 1

    def _cleanup(self, opaque):
        # Memory stays owned until player.stop/release has drained callbacks.
        pass

    def _video_lock(self, opaque, planes):
        self._lock.acquire()
        planes[0] = ctypes.cast(self._storage, ctypes.c_void_p).value
        return None

    def _unlock(self, opaque, picture, planes):
        self._lock.release()

    def _display(self, opaque, picture):
        try:
            with self._lock:
                if not self._active or self._storage is None:
                    return
                h, w = self._shape
                pixels = np.ctypeslib.as_array(self._storage).reshape(h, w, 4).copy()
                pixels.setflags(write=False)
                sample = FrameSample(self.session_id, self._seq + 1, time.monotonic(), pixels, self.display_aspect or w / h)
                self._seq += 1
            self.preview_frames.publish(sample)
        except Exception:
            # A failed copy drops this frame without publishing partial pixels.
            return

    def poll_frame(self):
        """VLC may repaint old pictures even through lock/unlock callbacks.

        Require independent decoder progress before accepting an output sample.
        Statistics are cached by VLC, so preview keeps its full callback cadence
        while detection consumes only conservatively verified output samples.
        """
        raw = self.preview_frames.latest()
        if not self.media or raw is None:
            return self.frames.latest()
        if not self.metadata_ready:
            self.refresh_metadata()
            if not self.metadata_ready:
                return self.frames.latest()
        stats = self.vlc.MediaStats()
        if self.media.get_stats(stats) and stats.decoded_video > self._decoded_count:
            previous = self.frames.latest()
            if previous is None or raw.frame_seq > previous.frame_seq:
                self._decoded_count = stats.decoded_video
                self.frames.publish(replace(raw, decoded_count=self._decoded_count, display_aspect=self.display_aspect))
        return self.frames.latest()

    def play(self, source: str):
        if self.media is not None:
            raise RuntimeError("A backend represents one session; create a new backend to reconnect")
        self.media = self.instance.media_new(source)
        self.player.set_media(self.media)
        self._active = True
        if self.player.play() == -1:
            self._active = False
            raise VlcError("media_open", "媒体打开失败，请检查地址及本机 VLC 支持能力。")

    def refresh_metadata(self):
        if not self.media:
            return
        selected_track = self.player.video_get_track()
        if selected_track < 0:
            return
        pointer = ctypes.POINTER(self.vlc.MediaTrack)()
        count = self.vlc.libvlc_media_tracks_get(self.media, ctypes.byref(pointer))
        if not count:
            return
        try:
            tracks = ctypes.cast(pointer, ctypes.POINTER(ctypes.POINTER(self.vlc.MediaTrack)))
            for i in range(count):
                track = tracks[i].contents
                if track.type != self.vlc.TrackType.video or track.id != selected_track:
                    continue
                # libVLC 3 uses a union here; the generated binding uses three
                # consecutive pointers. The first pointer is the correct union slot.
                video = ctypes.cast(track.audio, ctypes.POINTER(self.vlc.VideoTrack)).contents
                if video.width and video.height:
                    sar = video.sar_num / video.sar_den if video.sar_den else 1
                    self.display_aspect = video.width * sar / video.height
                    self.metadata_ready = True
                    self.metadata_error = ""
                    if video.orientation.value != 0:
                        self.metadata_error = "该视频含旋转/镜像元数据，尚未验证 ROI 映射；请转换为正常方向后使用。"
                break
        finally:
            self.vlc.libvlc_media_tracks_release(ctypes.cast(pointer, ctypes.POINTER(ctypes.POINTER(self.vlc.MediaTrack))), count)

    def status(self):
        return str(self.player.get_state()).split(".")[-1]

    def close(self):
        if self._closed:
            return
        self._active = False
        if not self._player_stopped:
            self.player.stop()
            self._player_stopped = True
        if not self._player_released:
            self.player.release()
            self._player_released = True
        if self.media is not None and not self._media_released:
            self.media.release()
            self._media_released = True
        if not self._instance_released:
            self.instance.release()
            self._instance_released = True
        self._closed = True
        self.frames.clear()
        self.preview_frames.clear()
        self._storage = None
        self._allocation = None
