"""Stable, UI-independent contracts shared by monitoring and persistence."""

from dataclasses import dataclass, field
import math
import uuid

from vlc_monitor.playback.frame_buffer import FrameSample


@dataclass
class RegionConfig:
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    name: str = "区域 1"
    rect: tuple[float, float, float, float] = (0.1, 0.1, 0.4, 0.4)
    enabled: bool = True
    black_enabled: bool = True
    freeze_enabled: bool = True
    black_count: int = 4
    freeze_count: int = 10
    recovery_count: int = 3
    black_level: float = 18
    black_ratio: float = 0.95
    freeze_delta: float = 3
    freeze_ratio: float = 0.98
    revision: int = 0

    def validate(self):
        if not isinstance(self.id, str) or not self.id or len(self.id) > 128:
            raise ValueError("区域 ID 无效")
        if not isinstance(self.name, str) or not self.name.strip() or len(self.name) > 80:
            raise ValueError("区域名称不能为空且最多 80 字")
        if len(self.rect) != 4 or any(type(v) not in (float, int) or not math.isfinite(v) for v in self.rect):
            raise ValueError("区域坐标无效")
        x, y, w, h = self.rect
        if min(x, y) < 0 or min(w, h) <= 0 or x + w > 1.0000001 or y + h > 1.0000001:
            raise ValueError("区域必须位于视频范围内")
        for key in ("enabled", "black_enabled", "freeze_enabled"):
            if type(getattr(self, key)) is not bool:
                raise ValueError("检测开关必须为布尔值")
        for key in ("black_count", "freeze_count", "recovery_count"):
            if type(getattr(self, key)) is not int or not 1 <= getattr(self, key) <= 3600:
                raise ValueError("连续次数必须为 1 到 3600 的整数")
        for key, low, high in (("black_level", 0, 255), ("black_ratio", 0.01, 1), ("freeze_delta", 0, 255), ("freeze_ratio", 0.01, 1)):
            value = getattr(self, key)
            if type(value) not in (float, int) or not math.isfinite(value) or not low <= value <= high:
                raise ValueError("像素阈值或占比无效")
        if type(self.revision) is not int or self.revision < 0:
            raise ValueError("区域修订号无效")


@dataclass
class AppConfig:
    schema_version: int = 2
    vlc_path: str = ""
    source: str = ""
    detection_interval: float = 1.0
    update_timeout: float = 5.0
    first_frame_timeout: float = 15.0
    repeat_seconds: float = 30.0
    sound_enabled: bool = True
    foreground_on_alarm: bool = True
    local_alert_interval_seconds: float = 5.0
    wechat_enabled: bool = False
    wechat_reminders_enabled: bool = False
    wechat_repeat_seconds: float = 60.0
    regions: list[RegionConfig] = field(default_factory=list)

    def validate(self):
        if type(self.schema_version) is not int or self.schema_version != 2:
            raise ValueError("不支持的配置版本")
        if not isinstance(self.source, str) or not isinstance(self.vlc_path, str):
            raise ValueError("地址和路径必须是文字")
        if len(self.source) > 8192 or len(self.vlc_path) > 4096:
            raise ValueError("地址或路径过长")
        for key in ("sound_enabled", "foreground_on_alarm", "wechat_enabled", "wechat_reminders_enabled"):
            if type(getattr(self, key)) is not bool:
                raise ValueError("通知开关无效")
        # Keep schema-2 compatibility without reactivating retired WeChat repeats.
        self.wechat_reminders_enabled = False
        for key, low, high in (("detection_interval", 0.2, 30), ("update_timeout", 2, 120), ("first_frame_timeout", 3, 120), ("repeat_seconds", 5, 3600), ("local_alert_interval_seconds", 3, 60), ("wechat_repeat_seconds", 60, 3600)):
            value = getattr(self, key)
            if type(value) not in (float, int) or not math.isfinite(value) or not low <= value <= high:
                raise ValueError("检测间隔或超时参数无效")
        if self.detection_interval >= self.update_timeout:
            raise ValueError("检测间隔必须小于视频更新超时")
        if len(self.regions) > 64 or len({r.id for r in self.regions}) != len(self.regions):
            raise ValueError("区域过多或 ID 重复")
        for region in self.regions:
            region.validate()


@dataclass
class AlarmEvent:
    id: str
    session_id: str
    region_id: str
    region_name: str
    anomaly_type: str  # black / freeze / stream / interruption / test
    revision: int
    started_at: float  # wall-clock seconds, for display/storage only
    triggered_at: float
    last_seen: float
    hit_count: int
    elapsed_seconds: float = 0.0
    ended_at: float | None = None
    end_reason: str = ""
    notification_result: str = ""
    reminder: bool = False
    run_id: int = 0


@dataclass(frozen=True)
class DetectionResult:
    region_id: str
    anomaly_type: str
    valid: bool
    score: float | None
    frame_seq: int
    revision: int
    hit: bool | None
