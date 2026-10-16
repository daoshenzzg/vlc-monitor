from dataclasses import asdict
import json
import os
from pathlib import Path
import tempfile
import shutil
import time

from vlc_monitor.models import AppConfig, RegionConfig


class ConfigError(ValueError):
    pass


class ConfigStore:
    def __init__(self, path):
        self.path = Path(path)

    def load(self):
        if not self.path.exists():
            return AppConfig()
        try:
            if self.path.stat().st_size > 1024 * 1024:
                raise ValueError("配置超过大小限制")
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                raise ValueError("配置格式错误")
            version = data.get("schema_version", 1)
            if type(version) is not int or version not in (1, 2):
                raise ValueError("不支持的配置版本")
            if version == 1:
                data["schema_version"] = 2
            regions = data.pop("regions", [])
            if not isinstance(regions, list) or len(regions) > 64:
                raise ValueError("区域列表无效")
            data["regions"] = [RegionConfig(**{**r, "rect": tuple(r["rect"]), "revision": 0}) for r in regions]
            config = AppConfig(**data)
            config.validate()
            return config
        except (OSError, ValueError, TypeError, KeyError, AttributeError) as exc:
            raise ConfigError("配置无法读取或内容无效；原文件已保留，请检查文件或明确保存新配置。") from exc

    def save(self, config):
        config.validate()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.exists():
            try:
                old = json.loads(self.path.read_text(encoding="utf-8"))
            except (ValueError, OSError):
                old = None
            if isinstance(old, dict) and old.get("schema_version", 1) == 1:
                backup = self.path.with_name(self.path.name + ".v1.bak")
                original = self.path.read_bytes()
                if backup.exists() and backup.read_bytes() != original:
                    backup = self.path.with_name(self.path.name + f".v1-{time.time_ns()}.bak")
                if not backup.exists():
                    # A failed backup must prevent migration from overwriting v1.
                    fd, staging = tempfile.mkstemp(prefix=".v1-backup-", dir=self.path.parent)
                    try:
                        with os.fdopen(fd, "wb") as stream:
                            stream.write(original)
                            stream.flush()
                            os.fsync(stream.fileno())
                        shutil.copystat(self.path, staging)
                        os.replace(staging, backup)
                    finally:
                        if os.path.exists(staging):
                            os.unlink(staging)
        data = asdict(config)
        for region in data["regions"]:
            region.pop("revision", None)
        descriptor, temporary = tempfile.mkstemp(prefix=".config-", suffix=".tmp", dir=self.path.parent)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                json.dump(data, stream, ensure_ascii=False, indent=2, allow_nan=False)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)


def redacted_config(config):
    allowed = ("schema_version", "vlc_path", "source", "detection_interval", "update_timeout", "first_frame_timeout", "repeat_seconds", "sound_enabled", "foreground_on_alarm", "local_alert_interval_seconds", "wechat_enabled", "wechat_reminders_enabled", "wechat_repeat_seconds", "regions")
    raw = asdict(config)
    data = {key: raw[key] for key in allowed}
    data["source"] = "[已脱敏]" if config.source else ""
    return data
