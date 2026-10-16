"""Native callback wrappers with bounded allocation fault injection."""
import ctypes
import threading

import pytest

from vlc_monitor.playback import vlc_backend as module
from vlc_monitor.playback.frame_buffer import LatestFrame


@pytest.fixture
def backend():
    backend = module.VlcBackend.__new__(module.VlcBackend)
    backend._lock = threading.Lock()
    backend._allocation = backend._storage = None
    backend._shape = (0, 0)
    backend._active = True
    backend._seq = 0
    backend.session_id = 'synthetic'
    backend.display_aspect = 0
    backend.preview_frames = LatestFrame()
    return backend


def call_format(backend, width, height):
    uint = ctypes.c_uint
    callback = ctypes.CFUNCTYPE(uint, ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(ctypes.c_char),
                               *([ctypes.POINTER(uint)] * 4))(backend._format)
    w, h, pitch, lines = map(uint, (width, height, 0, 0))
    chroma = ctypes.create_string_buffer(4)
    result = callback(None, chroma, ctypes.byref(w), ctypes.byref(h), ctypes.byref(pitch), ctypes.byref(lines))
    return result, chroma.raw, pitch.value, lines.value


@pytest.mark.parametrize('existing', [False, True])
def test_format_allocation_failure_is_atomic(backend, monkeypatch, existing):
    if existing:
        assert call_format(backend, 32, 16)[0] == 1
    old = backend._allocation, backend._storage, backend._shape
    class UnavailableByte:
        def __mul__(self, size):
            def fail():
                raise MemoryError('injected allocation failure')
            return fail
    monkeypatch.setattr(module.ctypes, 'c_ubyte', UnavailableByte())
    assert call_format(backend, 64, 32)[0] == 0
    assert backend._allocation is old[0] and backend._storage is old[1]
    assert backend._shape == old[2]


def test_format_resolution_switch_and_invalid_dimensions(backend):
    for w, h in ((32, 16), (64, 48)):
        assert call_format(backend, w, h) == (1, b'RV32', w * 4, h)
        assert backend._shape == (h, w)
        assert len(backend._storage) == w * h * 4
        assert ctypes.addressof(backend._storage) % 32 == 0
    old = backend._allocation
    for w, h in ((0, 32), (8193, 32), (32, 0), (32, 8193)):
        assert call_format(backend, w, h)[0] == 0
        assert backend._allocation is old and backend._shape == (48, 64)


def test_display_copy_failure_drops_frame_and_recovers(backend, monkeypatch):
    call_format(backend, 32, 16)
    display = ctypes.CFUNCTYPE(None, ctypes.c_void_p, ctypes.c_void_p)(backend._display)
    display(None, None)
    previous = backend.preview_frames.latest()
    with monkeypatch.context() as patch:
        patch.setattr(module.np.ctypeslib, 'as_array', lambda *args: (_ for _ in ()).throw(MemoryError('copy')))
        display(None, None)
    assert backend.preview_frames.latest() is previous and backend._seq == 1
    display(None, None)
    assert backend.preview_frames.latest().frame_seq == 2
    assert not backend.preview_frames.latest().pixels.flags.writeable
