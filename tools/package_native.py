"""Validate a native build, create fresh staging, record facts, and archive it."""
from __future__ import annotations
from importlib.metadata import version as package_version
import json
from pathlib import Path
import platform
import re
import shutil
import struct
import subprocess
import sys
import zipfile

from delivery import ROOT, audit_zip, checksums, version


def command(*args: str) -> str:
    return subprocess.run(args, check=True, capture_output=True, text=True).stdout.strip()


def native_facts(app: Path, target: str) -> dict:
    files = [p for p in app.rglob('*') if p.is_file()]
    if any(re.match(r'(?i)(libvlc.*\.(dll|dylib)|vlc\.dll)$', p.name) for p in files):
        raise ValueError('External VLC libraries found')
    if not any(p.name == 'alarm.wav' and p.stat().st_size > 44 for p in files):
        raise ValueError('Missing sound resource')
    plugin = 'libqcocoa.dylib' if target == 'macos-arm64' else 'qwindows.dll'
    if not any(p.name == plugin and 'platforms' in p.parts for p in files):
        raise ValueError('Missing Qt platform plugin')
    if target == 'macos-arm64':
        exe = app / 'Contents/MacOS/VLCMonitor'
        if command('lipo', '-archs', str(exe)) != 'arm64':
            raise ValueError('Executable is not ARM64')
        # Check all native Mach-O libraries, not just the launcher.
        count = 0
        for p in files:
            if p.is_symlink():
                continue
            kind = command('file', '-b', str(p))
            if 'Mach-O' in kind:
                if 'arm64' not in command('lipo', '-archs', str(p)).split():
                    raise ValueError(f'Native library lacks ARM64: {p.name}')
                count += 1
        command('codesign', '--verify', '--deep', '--strict', str(app))
        info = subprocess.run(['codesign', '-dvv', str(app)], check=True, capture_output=True, text=True).stderr
        if 'Signature=adhoc' not in info:
            raise ValueError('Expected verified ad-hoc signature; formal signing needs a separate delivery policy')
        signature = {'state': 'ad-hoc', 'verified_deep_strict': True, 'developer_id': False, 'notarized': False}
    else:
        exe = app / 'VLCMonitor.exe'
        count = 0
        for p in files:
            if p.suffix.lower() not in {'.exe', '.dll', '.pyd'}:
                continue
            with p.open('rb') as stream:
                if stream.read(2) != b'MZ':
                    raise ValueError(f'Invalid PE binary: {p.name}')
                stream.seek(0x3C)
                offset = struct.unpack('<I', stream.read(4))[0]
                stream.seek(offset)
                if stream.read(4) != b'PE\0\0' or struct.unpack('<H', stream.read(2))[0] != 0x8664:
                    raise ValueError(f'Non-x64 PE binary: {p.name}')
                count += 1
        if not exe.is_file():
            raise ValueError('Missing Windows launcher')
        state = command('pwsh', '-NoProfile', '-Command', f"(Get-AuthenticodeSignature -LiteralPath '{str(exe).replace(chr(39), chr(39)*2)}').Status.ToString()")
        if state != 'NotSigned':
            raise ValueError('Expected unsigned Windows build; signing needs a separate delivery policy')
        signature = {'state': 'unsigned', 'authenticode_status': state}
    return {'signature': signature, 'native_binaries_checked': count}


def main() -> None:
    value = version()
    mac = sys.platform == 'darwin'
    if not ((mac and platform.machine() == 'arm64') or (sys.platform == 'win32' and platform.machine().lower() in {'amd64', 'x86_64'})) or struct.calcsize('P') != 8:
        raise ValueError('Native ARM64 macOS / x64 Windows with 64-bit Python required')
    target = 'macos-arm64' if mac else 'windows-x64'
    app = ROOT / 'dist/native' / ('VLCMonitor.app' if mac else 'VLCMonitor')
    facts = native_facts(app, target)
    sys.path.insert(0, str(ROOT / 'packaging'))
    from collect_notices import collect, TARGET
    packages = collect()
    git = subprocess.run(['git', '-C', str(ROOT), 'rev-parse', '--show-toplevel'], capture_output=True, text=True)
    if git.returncode == 0 and Path(git.stdout.strip()).resolve() == ROOT:
        head = subprocess.run(['git', '-C', str(ROOT), 'rev-parse', '--verify', 'HEAD'], capture_output=True, text=True)
        sha = head.stdout.strip() if head.returncode == 0 else None
        dirty = not sha or bool(command('git', '-C', str(ROOT), 'status', '--porcelain'))
    else:
        sha, dirty = None, None
    manifest = {'application': value, 'target': target, 'architecture': platform.machine(),
                'python': platform.python_version(), 'pyinstaller': package_version('PyInstaller'),
                'git_sha': sha, 'working_tree_dirty': dirty, 'packages': packages,
                'vlc': 'External; not redistributed', **facts,
                'acceptance': 'Build/static validation only; playback, sound and notifications require target-device acceptance'}
    stage = ROOT / f'build/delivery/VLCMonitor-{value}-{target}-internal'
    if stage.is_symlink():
        raise ValueError('Staging may not be a symlink')
    if stage.exists(): shutil.rmtree(stage)
    stage.mkdir(parents=True)
    if mac: command('ditto', str(app), str(stage / app.name))
    else: shutil.copytree(app, stage / app.name)
    shutil.copy2(ROOT / 'README.md', stage / 'README.md')
    docs = stage / 'docs'
    docs.mkdir()
    shutil.copy2(ROOT / 'docs/THIRD_PARTY_NOTICES.md', docs / 'THIRD_PARTY_NOTICES.md')
    for relative in ('docs/usage.md', 'docs/images/app-overview.png', 'docs/plans/2026-10-16-implementation.md'):
        destination = stage / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / relative, destination)
    shutil.copytree(TARGET, docs / 'third-party-licenses')
    (docs / 'build-manifest.json').write_text(json.dumps(manifest, indent=2) + '\n', encoding='utf-8')
    output = ROOT / f'dist/{stage.name}.zip'
    if output.exists(): output.unlink()
    if mac: command('ditto', '-c', '-k', '--keepParent', str(stage), str(output))
    else:
        with zipfile.ZipFile(output, 'w', zipfile.ZIP_DEFLATED) as archive:
            for p in sorted(stage.rglob('*')):
                if p.is_file(): archive.write(p, p.relative_to(stage.parent).as_posix())
    audit_zip(output)
    with zipfile.ZipFile(output) as archive:
        names = archive.namelist()
        for suffix in ('/docs/build-manifest.json', '/docs/THIRD_PARTY_NOTICES.md', '/README.md'):
            if not any(n.endswith(suffix) for n in names): raise ValueError('Incomplete platform archive')
        if not any('/docs/third-party-licenses/' in n for n in names): raise ValueError('Missing licenses')
    checksums([output], ROOT / f'dist/SHA256SUMS-{value}-{target}.txt')
    print(output.name)


if __name__ == '__main__': main()
