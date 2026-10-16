from pathlib import Path
import time


class SoundPolicy:
    def __init__(self, cooldown=3.0):
        self.muted_until = 0.0
        self.last_sound = -float("inf")
        self.cooldown = cooldown

    def request(self, now):
        if now < self.muted_until or now - self.last_sound < self.cooldown:
            return False
        self.last_sound = now
        return True

    def mute(self, now, duration=300):
        self.muted_until = now + duration

    def unmute(self):
        self.muted_until = 0.0


class LocalSound:
    def __init__(self, parent=None):
        from PySide6.QtCore import QUrl
        from PySide6.QtMultimedia import QSoundEffect
        self.policy = SoundPolicy()
        self.effect = QSoundEffect(parent)
        self.effect.setSource(QUrl.fromLocalFile(str(Path(__file__).resolve().parents[1] / "assets/alarm.wav")))
        self.effect.setVolume(0.65)

    def play(self):
        from PySide6.QtMultimedia import QSoundEffect
        if not self.policy.request(time.monotonic()):
            return "静音或声音合并限频中"
        if self.effect.status() == QSoundEffect.Status.Error:
            return "音效加载失败，请检查音频输出"
        if not self.effect.isPlaying():
            self.effect.play()
        return "已请求播放提示音（请确认扬声器可听）"
