#!/bin/sh
set -eu
cd "$(dirname "$0")/.."
test "$(uname -s)" = Darwin && test "$(uname -m)" = arm64
uv sync --locked --extra dev
.venv/bin/python tools/delivery.py version
.venv/bin/python tools/build_paths.py
.venv/bin/pyinstaller --clean --noconfirm --distpath dist/native --workpath build/pyinstaller packaging/VLCMonitor.spec
.venv/bin/python tools/package_native.py
printf '%s\n' 'Internal ad-hoc build. No Developer ID/notarization or target-device acceptance.'
