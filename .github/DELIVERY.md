# Delivery Controls

Run from this independent repository root (Python 3.12.13, uv 0.12.18):

```sh
uv sync --locked --extra dev
uv run --no-sync python tools/delivery.py audit
uv run --no-sync python tools/delivery.py scan
uv run --no-sync python packaging/source_archive.py
```

Native builds run separately: `sh packaging/build-macos.sh` on native ARM64 macOS,
`pwsh -File packaging/build-windows.ps1` on native x64 Windows. Neither creates a
source archive. Source is generated once by the source job. Platform ZIPs contain
only the application, README, notices, original dependency licenses and a platform
manifest; they do not contain historical diagnostics or a nested source ZIP.
Generated licenses and manifests live under ignored `build/`, not `docs/`.

The source whitelist is enforced by `tools/delivery.py`. Unknown non-generated
paths fail closed; ignored build, virtualenv, tmp, cache and local UI-preview
outputs are not included. When the independent Git repository exists, tracked
paths outside the whitelist also fail (ignore rules cannot conceal a tracked
secret). Symbolic links are forbidden in source. Source ZIP entries and bytes are
compared with the whitelist. Platform ZIP paths, symbolic links, CRC and forbidden
runtime data/library categories are checked before upload. Every native PE/Mach-O
binary is checked for the target architecture, and the Qt platform plugin, alarm
sound, licenses and actual signature are mandatory. macOS ad-hoc verification is
not Developer ID signing/notarization. Windows NotSigned is measured, not inferred.

`detect-secrets==1.5.0` runs locally and in CI against the same whitelist. Scanner
stdout/stderr is captured and only file paths/detector categories reach reports.
The reviewed allowlist is exact path + detector + hashed synthetic test marker;
it does not exempt whole files. Scanner failures are fatal. It is not an audit
of arbitrary pre-existing Git history, image content, source ownership, or every
possible private URL. Review staged content and the final screenshot manually;
if this repository ever gains imported history, audit that history before push.
No scanning success authorizes uploading private material.

All Actions are pinned to full upstream commits; checkout credentials are not
persisted. PRs run source audit, secret scanning and tests only, without publishing
permissions. Main/manual runs build both native targets. Only matching `vX.Y.Z`
tag pushes can publish, after source and both native jobs succeed. Release input
is exactly three named ZIPs plus the three platform/source checksum reports.
Checksums and platform manifests are reverified against the tagged SHA. The
publisher refuses any existing release, including drafts; it never overwrites
assets. A failed draft must be manually reviewed/removed before retrying, or use a
new version. GitHub repository-level immutable-release enforcement is an owner
setting, not silently configured by this workflow. Retention is 14 days for
Actions artifacts; private-repository availability and billing remain owner checks.

## Public Metadata Verified 2026-10-16

Firecrawl skill/CLI was attempted first, but this machine was not authenticated;
no login or key was requested. Public upstream HTTP/Git reads were used instead.

- [GitHub hosted runner table](https://docs.github.com/en/actions/reference/runners/github-hosted-runners)
  maps `macos-15` to ARM64 (and `macos-15-intel` to Intel). The workflow additionally
  rejects any runtime CPU/bitness mismatch. `windows-2022` is the x64 target.
- [uv 0.12.18 source tag](https://github.com/astral-sh/uv/tree/0.12.18) and release
  download endpoints for `uv-aarch64-apple-darwin.tar.gz` and
  `uv-x86_64-pc-windows-msvc.zip` responded successfully. Native execution on both
  hosted runners is still pending.
- `uv python list --all-versions --all-platforms --all-arches` lists managed
  CPython 3.12.13 for macOS aarch64 and Windows x86_64. Installation is explicit;
  no fallback Python patch is permitted. See
  [python-build-standalone](https://github.com/astral-sh/python-build-standalone).
- Full action SHAs were resolved from upstream tag refs with Git or GitHub API:
  [checkout v4](https://github.com/actions/checkout/tree/11d5960a326750d5838078e36cf38b85af677262),
  [upload-artifact v4](https://github.com/actions/upload-artifact/tree/ea165f8d65b6e75b540449e92b4886f43607fa02),
  [download-artifact v4](https://github.com/actions/download-artifact/tree/d3f86a106a0bac45b974a628896c90dbdf5c8093),
  [setup-uv v6](https://github.com/astral-sh/setup-uv/tree/d0d8abe699bfb85fec6de9f7adb5ae17292296ff).
- [detect-secrets 1.5.0](https://pypi.org/project/detect-secrets/1.5.0/) was installed
  and its version checked locally. Its transitive dependencies are not a separately
  locked application dependency set.
- `packaging/licenses/Python-LICENSE.txt` is the unmodified
  [CPython 3.12.13 license](https://github.com/python/cpython/blob/v3.12.13/LICENSE).

These public availability checks do not prove that a particular private account
can provision runners, nor that Windows/macOS native CI or device acceptance has
succeeded. No remote repository, push, tag or release was created by these changes.

## Qt License Collection Limitation

The installed PySide6, PySide6-Essentials, PySide6-Addons and shiboken6 6.8.3 wheel
RECORDs contain no LICENSE/COPYING files in this environment. Their actual license
expressions are recorded, and four unmodified texts from
[qtbase v6.8.3 LICENSES](https://github.com/qt/qtbase/tree/v6.8.3/LICENSES) are
supplemented from audited `packaging/licenses/Qt-6.8.3-*.txt` files (LGPL-3.0-only,
GPL-2.0-only, GPL-3.0-only, Qt-GPL-exception-1.0). Each affected package has an
explicit COLLECTION-NOTE rather than pretending those texts came from the wheel.
Updating Qt requires reviewing the supplements. These texts do not establish
complete bundled Qt/FFmpeg third-party attribution or corresponding-source
compliance; public/commercial distribution remains blocked on owner/legal review.
