"""Find an external VLC without importing the binding prematurely."""

from dataclasses import dataclass
from pathlib import Path
import ctypes
import os
import platform
import struct
import sys
import threading


class VlcError(RuntimeError):
    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


@dataclass(frozen=True)
class VlcLocation:
    root: Path
    library: Path
    plugins: Path


def architectures(path: Path) -> set[str]:
    try:
        return _read_architectures(path)
    except (OSError, struct.error):
        return set()


def _read_architectures(path: Path) -> set[str]:
    with path.open("rb") as stream:
        head = stream.read(4096)
        if head[:2] == b"MZ":
            offset = struct.unpack_from("<I", head, 0x3C)[0]
            stream.seek(offset)
            pe = stream.read(6)
            if pe[:4] != b"PE\0\0":
                return set()
            return { {0x8664: "x86_64", 0x14C: "x86", 0xAA64: "arm64"}.get(struct.unpack_from("<H", pe, 4)[0], "unknown") }
    cpus = {0x100000C: "arm64", 0x1000007: "x86_64"}
    if head[:4] == b"\xcf\xfa\xed\xfe":
        return {cpus.get(struct.unpack_from("<I", head, 4)[0], "unknown")}
    if head[:4] in (b"\xca\xfe\xba\xbe", b"\xca\xfe\xba\xbf"):
        count = struct.unpack_from(">I", head, 4)[0]
        stride = 32 if head[3] == 0xBF else 20
        return {cpus.get(struct.unpack_from(">I", head, 8 + i * stride)[0], "unknown") for i in range(min(count, 32))}
    return set()


def locate(selected: str = "") -> VlcLocation:
    if sys.platform == "darwin":
        if platform.machine() != "arm64":
            raise VlcError("unsupported", "本版本仅支持 Apple Silicon Mac。")
        candidates = [Path(selected)] if selected else [Path("/Applications/VLC.app"), Path.home() / "Applications/VLC.app"]
        expected = "arm64"
    elif sys.platform == "win32":
        if platform.machine().lower() not in ("amd64", "x86_64") or ctypes.sizeof(ctypes.c_void_p) != 8:
            raise VlcError("architecture", "请使用 Windows x64 程序和 x64 VLC。")
        candidates = [Path(selected)] if selected else [Path(os.environ.get("PROGRAMFILES", "C:/Program Files")) / "VideoLAN/VLC", Path("C:/Program Files/VideoLAN/VLC")]
        expected = "x86_64"
    else:
        raise VlcError("unsupported", "仅支持 Windows 10/11 x64 和 Apple Silicon Mac。")
    failures = []
    for root in candidates:
        base = root / "Contents/MacOS" if sys.platform == "darwin" else root
        lib = base / "lib/libvlc.dylib" if sys.platform == "darwin" else base / "libvlc.dll"
        core = base / "lib/libvlccore.dylib" if sys.platform == "darwin" else base / "libvlccore.dll"
        if not lib.is_file() or not core.is_file():
            failures.append(("invalid_path", "VLC 路径无效或缺少动态库，请选择完整 VLC 安装目录。"))
            continue
        if any(expected not in architectures(p) for p in (lib, core)):
            failures.append(("architecture", f"VLC 架构不匹配，需要 {expected} 版本。"))
            continue
        plugins = base / "plugins"
        pattern = "*_plugin.dll" if sys.platform == "win32" else "*_plugin.dylib"
        if not plugins.is_dir() or not any(plugins.rglob(pattern)):
            failures.append(("plugins", "VLC 插件目录缺失，请重新安装完整 VLC。"))
            continue
        vmem = next((p for p in plugins.rglob("*vmem_plugin.*") if p.is_file()), None)
        if vmem is None:
            failures.append(("plugins", "缺少 VLC 内存视频输出插件，请安装完整 VLC。"))
            continue
        if expected not in architectures(vmem):
            failures.append(("architecture", "VLC 内存视频插件架构不匹配，请重新安装匹配架构的完整 VLC。"))
            continue
        return VlcLocation(root.resolve(), lib.resolve(), plugins.resolve())
    if not selected and not any(p.exists() for p in candidates):
        raise VlcError("not_installed", "未找到 VLC。请安装匹配架构的 VLC，或选择已有安装位置。")
    code, message = failures[-1]
    raise VlcError(code, message)


_dll_handles = []
_loaded_location = None
_binding = None
_load_lock = threading.Lock()
_load_tainted = False


def load_binding(location: VlcLocation):
    location = VlcLocation(*(path.resolve() for path in (location.root, location.library, location.plugins)))
    with _load_lock:
        return _load_binding(location)


def _load_binding(location):
    global _loaded_location, _binding, _load_tainted
    if _load_tainted:
        raise VlcError("restart", "VLC 动态库部分装载失败，请重启程序后重试。")
    if _loaded_location is not None and _loaded_location != location:
        raise VlcError("restart", "修改 VLC 路径后请保存配置并重启程序。")
    if _binding is not None:
        return _binding
    paths = {"PYTHON_VLC_LIB_PATH": str(location.library),
             "PYTHON_VLC_MODULE_PATH": str(location.plugins), "VLC_PLUGIN_PATH": str(location.plugins)}
    previous = {key: os.environ.get(key) for key in paths}
    directory = None
    libraries = []
    try:
        os.environ.update(paths)
        if sys.platform == "win32":
            directory = os.add_dll_directory(str(location.library.parent))
        # VLC.app uses @rpath for its core; Python's executable has no VLC rpath.
        if sys.platform == "darwin":
            libraries.append(ctypes.CDLL(str(location.library.parent / "libvlccore.dylib"), mode=ctypes.RTLD_GLOBAL))
        libraries.append(ctypes.CDLL(str(location.library)))
        import vlc
        vlc.libvlc_get_version()
    except (Exception, SystemExit) as exc:
        # Retain partial native loads; unloading references is not a safe rollback.
        _dll_handles.extend(libraries)
        _load_tainted = bool(libraries)
        if directory is not None:
            directory.close()
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        if _load_tainted:
            raise VlcError("restart", "VLC 动态库部分装载失败，请重启程序后重试。") from exc
        raise VlcError("load_failed", "VLC 动态库装载失败，请检查安装完整性与架构。") from exc
    _dll_handles.extend(libraries)
    if directory is not None:
        _dll_handles.append(directory)
    _loaded_location = location
    _binding = vlc
    return vlc
