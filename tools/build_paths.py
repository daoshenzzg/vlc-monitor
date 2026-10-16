"""Prepare only the controlled native work/output paths, never user data."""
from pathlib import Path
import shutil
from delivery import ROOT

for relative in ('build', 'dist', 'build/pyinstaller', 'build/delivery', 'dist/native'):
    path = ROOT / relative
    if path.is_symlink() or not path.resolve().is_relative_to(ROOT):
        raise ValueError(f'Generated path escapes checkout: {relative}')
for relative in ('build/pyinstaller', 'dist/native'):
    path = ROOT / relative
    if path.exists():
        if not path.is_dir(): raise ValueError(f'Expected generated directory: {relative}')
        shutil.rmtree(path)
