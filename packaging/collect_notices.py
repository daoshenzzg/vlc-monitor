"""Collect installed distribution notices into a fresh, generated build directory."""
from importlib.metadata import distribution, version
from pathlib import Path
import shutil

ROOT = Path(__file__).resolve().parents[1]
TARGET = ROOT / "build/delivery/licenses"
PACKAGES = ["qrcode", "PySide6", "PySide6-Essentials", "PySide6-Addons", "shiboken6", "python-vlc", "numpy", "opencv-python-headless", "psutil", "PyInstaller"]


def collect() -> dict[str, str]:
    if TARGET.is_symlink() or TARGET.parent.is_symlink():
        raise ValueError("Generated license destination may not be a symlink")
    if TARGET.exists():
        shutil.rmtree(TARGET)
    TARGET.mkdir(parents=True)
    shutil.copy2(ROOT / "packaging/licenses/Python-LICENSE.txt", TARGET / "Python-LICENSE.txt")
    versions = {}
    for package in PACKAGES:
        dist = distribution(package)
        versions[package] = version(package)
        destination = TARGET / package
        destination.mkdir()
        license_value = dist.metadata.get("License-Expression") or dist.metadata.get("License") or "See installed license files"
        (destination / "METADATA-LICENSE.txt").write_text(license_value, encoding="utf-8")
        copied = 0
        for path in dist.files or []:
            if not any(part.lower().startswith(("license", "copying", "copyright")) for part in path.parts):
                continue
            source = Path(dist.locate_file(path))
            if source.is_file():
                output = destination.joinpath(*[p for p in path.parts if p not in ("..", ".")])
                output.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, output)
                copied += 1
        if not copied and package in {'PySide6', 'PySide6-Essentials', 'PySide6-Addons', 'shiboken6'}:
            if versions[package] != '6.8.3':
                raise ValueError('Qt license supplements must be reviewed for the new version')
            for suffix in ('LGPL-3.0-only', 'GPL-2.0-only', 'GPL-3.0-only', 'Qt-GPL-exception-1.0'):
                source = ROOT / f'packaging/licenses/Qt-6.8.3-{suffix}.txt'
                shutil.copy2(source, destination / source.name)
                copied += 1
            (destination / 'COLLECTION-NOTE.txt').write_text(
                'The installed 6.8.3 wheel RECORD contains no original license files. '
                'Supplemented from upstream qt/qtbase v6.8.3 LICENSES; see .github/DELIVERY.md in source. '
                'This is not a complete audit of all Qt/FFmpeg third-party attributions or corresponding-source obligations.\n',
                encoding='utf-8')
        if not copied:
            raise ValueError(f"No original license files found for {package}")
    return versions


if __name__ == "__main__":
    collect()
