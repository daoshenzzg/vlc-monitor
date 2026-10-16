from dataclasses import dataclass
import threading
import numpy as np


@dataclass(frozen=True)
class FrameSample:
    session_id: str
    frame_seq: int
    received_at: float
    pixels: np.ndarray  # BGRA, immutable after publication
    display_aspect: float
    media_time_ms: int = -1
    decoded_count: int = 0

    @property
    def width(self):
        return self.pixels.shape[1]

    @property
    def height(self):
        return self.pixels.shape[0]

    @property
    def stride(self):
        return self.pixels.strides[0]


class LatestFrame:
    def __init__(self):
        self._lock = threading.Lock()
        self._frame = None

    def publish(self, sample: FrameSample):
        with self._lock:
            self._frame = sample

    def latest(self):
        with self._lock:
            return self._frame

    def clear(self):
        with self._lock:
            self._frame = None
