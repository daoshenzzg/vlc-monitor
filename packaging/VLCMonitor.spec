# Build natively; libVLC remains a separately installed runtime dependency.
from pathlib import Path
import sys
import tomllib

root = Path(SPECPATH).parent
version = tomllib.loads((root / 'pyproject.toml').read_text(encoding='utf-8'))['project']['version']
a = Analysis([str(root / 'packaging/launcher.py')], pathex=[str(root / 'src')],
             binaries=[], datas=[(str(root / 'src/vlc_monitor/assets/alarm.wav'), 'vlc_monitor/assets')],
             hiddenimports=['qrcode', 'PySide6.QtNetwork'],
             excludes=['PySide6.QtWebEngineCore', 'PySide6.QtWebEngineWidgets'], noarchive=False)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name='VLCMonitor',
          debug=False, bootloader_ignore_signals=False, strip=False,
          upx=False, console=False, target_arch='arm64' if sys.platform == 'darwin' else None)
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name='VLCMonitor')
if sys.platform == 'darwin':
    app = BUNDLE(coll, name='VLCMonitor.app', icon=None,
                 bundle_identifier='local.tools.vlcmonitor',
                 info_plist={'CFBundleShortVersionString': version, 'CFBundleVersion': version,
                             'NSHighResolutionCapable': True, 'LSMinimumSystemVersion': '13.0'})
