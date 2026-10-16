"""Process-local binding initialization, without loading native libraries."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
import sys
import time

import pytest

from vlc_monitor.platform import vlc_locator as module


@pytest.fixture
def loader(monkeypatch, tmp_path):
    monkeypatch.setattr(module, '_dll_handles', [])
    monkeypatch.setattr(module, '_loaded_location', None)
    monkeypatch.setattr(module, '_binding', None)
    monkeypatch.setattr(module, '_load_tainted', False)
    for key in ('PYTHON_VLC_LIB_PATH', 'PYTHON_VLC_MODULE_PATH', 'VLC_PLUGIN_PATH'):
        monkeypatch.delenv(key, raising=False)
    calls, directories = [], []
    def cdll(path, **kwargs):
        calls.append((path, kwargs))
        time.sleep(.001)
        return object()
    def add_directory(path):
        handle = SimpleNamespace(closed=False)
        handle.close = lambda: setattr(handle, 'closed', True)
        directories.append(handle)
        return handle
    monkeypatch.setattr(module.ctypes, 'CDLL', cdll)
    monkeypatch.setattr(module.os, 'add_dll_directory', add_directory, raising=False)
    binding = SimpleNamespace(libvlc_get_version=lambda: b'test')
    monkeypatch.setitem(sys.modules, 'vlc', binding)
    location = module.VlcLocation(tmp_path, tmp_path / 'libvlc.dll', tmp_path / 'plugins')
    return location, binding, calls, directories


def test_windows_same_location_cached_serially_and_change_requires_restart(loader, monkeypatch):
    location, binding, calls, directories = loader
    monkeypatch.setattr(module.sys, 'platform', 'win32')
    equivalent = module.VlcLocation(location.root / '.', location.library, location.plugins)
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(module.load_binding, [equivalent] * 100))
    assert all(result is binding for result in results)
    assert len(calls) == len(directories) == 1
    assert len(module._dll_handles) == 2 and not directories[0].closed
    other = module.VlcLocation(Path('/other'), Path('/other/libvlc.dll'), Path('/other/plugins'))
    with pytest.raises(module.VlcError, match='重启') as caught:
        module.load_binding(other)
    assert caught.value.code == 'restart' and len(calls) == 1


def test_failed_initial_load_closes_search_directory_and_allows_retry(loader, monkeypatch):
    location, binding, calls, directories = loader
    monkeypatch.setattr(module.sys, 'platform', 'win32')
    cdll = module.ctypes.CDLL
    with monkeypatch.context() as patch:
        patch.setattr(module.ctypes, 'CDLL', lambda *args: (_ for _ in ()).throw(OSError('missing')))
        with pytest.raises(module.VlcError) as caught:
            module.load_binding(location)
    assert caught.value.code == 'load_failed'
    assert directories[0].closed and module._dll_handles == []
    assert module._binding is None and module._loaded_location is None
    assert 'PYTHON_VLC_LIB_PATH' not in module.os.environ
    assert module.load_binding(location) is binding
    assert len(directories) == 2 and len(module._dll_handles) == 2


def test_partial_native_load_requires_restart_without_repeated_loads(loader, monkeypatch):
    location, binding, calls, directories = loader
    monkeypatch.setattr(module.sys, 'platform', 'win32')
    def fail():
        raise NameError('incomplete binding')
    binding.libvlc_get_version = fail
    for _ in range(2):
        with pytest.raises(module.VlcError) as caught:
            module.load_binding(location)
        assert caught.value.code == 'restart'
    assert directories[0].closed and len(directories) == len(calls) == 1
    assert len(module._dll_handles) == 1 and module._binding is None


def test_macos_loads_core_globally_before_library(loader, monkeypatch):
    location, binding, calls, directories = loader
    monkeypatch.setattr(module.sys, 'platform', 'darwin')
    assert module.load_binding(location) is binding
    assert calls == [(str(location.library.parent / 'libvlccore.dylib'), {'mode': module.ctypes.RTLD_GLOBAL}),
                     (str(location.library), {})]
    assert directories == [] and len(module._dll_handles) == 2
