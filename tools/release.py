"""Publish only a complete, checksum-verified, matching-tag candidate. CI only."""
from pathlib import Path
import json
import os
import subprocess
import zipfile

from delivery import ROOT, audit_zip, checksums, verify_checksums, version

value = version()
tag = os.environ['RELEASE_TAG']
if tag != f'v{value}':
    raise ValueError('Tag does not match project/module version')
if not os.environ.get('GITHUB_REPOSITORY') or not os.environ.get('GITHUB_SHA'):
    raise ValueError('Release requires GitHub CI identity')
files = [ROOT / f'dist/VLCMonitor-{value}-{target}.zip' for target in ('source', 'macos-arm64-internal', 'windows-x64-internal')]
expected = {p.name for p in files} | {f'SHA256SUMS-{value}-{target}.txt' for target in ('source', 'macos-arm64', 'windows-x64')}
if {p.name for p in (ROOT / 'dist').iterdir()} != expected:
    raise ValueError('Release input must contain exactly three ZIPs and three checksum files')
for target, artifact in zip(('source', 'macos-arm64', 'windows-x64'), files):
    verify_checksums(ROOT / f'dist/SHA256SUMS-{value}-{target}.txt', {artifact.name})
for path in files:
    audit_zip(path, source=path == files[0])
    if path != files[0]:
        with zipfile.ZipFile(path) as archive:
            manifests = [n for n in archive.namelist() if n.endswith('/docs/build-manifest.json')]
            if len(manifests) != 1: raise ValueError('Missing platform manifest')
            manifest = json.loads(archive.read(manifests[0]))
            if manifest['application'] != value or manifest['git_sha'] != os.environ['GITHUB_SHA'] or manifest['working_tree_dirty']:
                raise ValueError('Platform manifest is not from the clean tagged commit')
            expected_target = 'macos-arm64' if 'macos-arm64' in path.name else 'windows-x64'
            if manifest['target'] != expected_target:
                raise ValueError('Wrong platform manifest')
summary = ROOT / f'dist/SHA256SUMS-{value}.txt'
checksums(files, summary)
verify_checksums(summary, {path.name for path in files})
# Existing drafts and published versions both fail closed. Never overwrite assets.
lookup = subprocess.run(['gh', 'release', 'view', tag, '--json', 'isDraft'], capture_output=True, text=True)
if lookup.returncode == 0:
    raise ValueError('Release already exists: use a new version; no mutation permitted')
notes = ('Native macOS ARM64 ad-hoc and Windows x64 unsigned build candidates. '
         'External matching-architecture VLC installation required. No macOS notarization. '
         'CI build/static checks are not Windows 10/11 or macOS playback, sound, credential or notification acceptance. '
         'Source authorization and dependency compliance require owner review. '
         'Published versions are not overwritten; corrections require a new version.')
subprocess.run(['gh', 'release', 'create', tag, '--verify-tag', '--draft', '--title', f'VLC Monitor {value}', '--notes', notes, *map(str, files), str(summary)], check=True)
subprocess.run(['gh', 'release', 'edit', tag, '--draft=false'], check=True)
