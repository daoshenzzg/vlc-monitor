"""Shared, fail-closed delivery contracts. Run only from an audited checkout."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys
import tomllib
import zipfile

ROOT = Path(__file__).resolve().parents[1]
FILES = {".gitignore", ".python-version", "pyproject.toml", "uv.lock", "README.md",
         "docs/THIRD_PARTY_NOTICES.md", "docs/usage.md", "docs/images/app-overview.png",
         "docs/plans/2026-10-16-github-delivery.md", "docs/plans/2026-10-16-app-ui-simplification.md",
         "docs/plans/2026-10-16-stability.md", "packaging/licenses/Python-LICENSE.txt",
         "docs/plans/2026-10-16-implementation.md",
         "packaging/licenses/Qt-6.8.3-LGPL-3.0-only.txt", "packaging/licenses/Qt-6.8.3-GPL-2.0-only.txt",
         "packaging/licenses/Qt-6.8.3-GPL-3.0-only.txt", "packaging/licenses/Qt-6.8.3-Qt-GPL-exception-1.0.txt",
         ".github/secret-allowlist.json", ".github/DELIVERY.md"}
TREES = {"src/vlc_monitor": {".py", ".wav"}, "tests": {".py"},
         "tools": {".py"}, "packaging": {".py", ".spec", ".sh", ".ps1"},
         ".github/workflows": {".yml", ".yaml"}}
REQUIRED = {"pyproject.toml", "uv.lock", ".python-version", "README.md", ".gitignore",
            "src/vlc_monitor/__init__.py", "src/vlc_monitor/assets/alarm.wav",
            "docs/THIRD_PARTY_NOTICES.md"}


def version() -> str:
    value = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]
    if not re.fullmatch(r"\d+\.\d+\.\d+(?:[a-zA-Z0-9.+-]*)?", value):
        raise ValueError("Invalid project version")
    text = (ROOT / "src/vlc_monitor/__init__.py").read_text(encoding="utf-8")
    match = re.search(r'__version__\s*=\s*[\"\']([^\"\']+)', text)
    if not match or match.group(1) != value:
        raise ValueError("Project and module versions differ")
    return value


def source_files() -> list[Path]:
    paths = []
    for path in ROOT.rglob("*"):
        rel = path.relative_to(ROOT)
        if rel.parent.as_posix() == 'docs/images' and rel.name.startswith('ui-preview-') and rel.suffix == '.png':
            continue
        if any(part in {".git", ".venv", ".uv-cache", "__pycache__", ".pytest_cache", "build", "dist", "tmp", ".firecrawl"} or part.endswith('.egg-info') for part in rel.parts):
            continue
        if rel.name == '.DS_Store':
            continue
        # Inspect links even when their target is missing or a directory.
        if path.is_symlink():
            raise ValueError(f"Symlink forbidden in source: {rel.as_posix()}")
        if not path.is_file():
            continue
        name = rel.as_posix()
        accepted = name in FILES or any(name.startswith(tree + "/") and path.suffix in suffixes for tree, suffixes in TREES.items())
        if not accepted:
            raise ValueError(f"Unreviewed source path: {name}")
        if path.stat().st_size > 5_000_000:
            raise ValueError(f"Oversized source requires review: {name}")
        paths.append(path)
    missing = REQUIRED - {p.relative_to(ROOT).as_posix() for p in paths}
    if missing:
        raise ValueError(f"Missing required source: {sorted(missing)}")
    git_root = subprocess.run(['git', '-C', str(ROOT), 'rev-parse', '--show-toplevel'], capture_output=True, text=True)
    if git_root.returncode == 0 and Path(git_root.stdout.strip()).resolve() == ROOT:
        tracked = subprocess.run(['git', '-C', str(ROOT), 'ls-files', '-z'], check=True, capture_output=True).stdout.decode().split('\0')
        unexpected = set(filter(None, tracked)) - {p.relative_to(ROOT).as_posix() for p in paths}
        if unexpected:
            raise ValueError(f'Tracked files outside source whitelist: {sorted(unexpected)}')
    return sorted(paths)


def checksums(files: list[Path], output: Path) -> None:
    if not files:
        raise ValueError("Cannot generate an empty checksum report")
    if len({p.name for p in files}) != len(files):
        raise ValueError("Duplicate checksum basenames")
    output.write_text("".join(f"{hashlib.sha256(p.read_bytes()).hexdigest()}  {p.name}\n" for p in sorted(files)), encoding="utf-8")


def verify_checksums(report: Path, expected_basenames: set[str] | None = None) -> None:
    lines = report.read_text(encoding="utf-8").splitlines()
    if not lines:
        raise ValueError("Empty checksum report")
    entries = {}
    for line in lines:
        match = re.fullmatch(r"([0-9a-f]{64})  (.+)", line)
        if not match:
            raise ValueError("Invalid checksum entry")
        digest, name = match.groups()
        if Path(name).name != name or name in {'.', '..'} or any(c in name for c in '/\\:'):
            raise ValueError("Invalid checksum basename")
        if name in entries:
            raise ValueError(f"Duplicate checksum basename: {name}")
        entries[name] = digest
    if expected_basenames is not None and set(entries) != expected_basenames:
        raise ValueError("Checksum report does not cover exactly the expected artifacts")
    for name, digest in entries.items():
        if hashlib.sha256((report.parent / name).read_bytes()).hexdigest() != digest:
            raise ValueError(f"Checksum mismatch: {name}")


def audit_zip(path: Path, source: bool = False) -> None:
    with zipfile.ZipFile(path) as archive:
        names = archive.namelist()
        if len(names) != len(set(names)):
            raise ValueError("Duplicate archive entries")
        for item in archive.infolist():
            name = item.filename
            parts = Path(name).parts
            if name.startswith(("/", "\\")) or ".." in parts or "\\" in name or ':' in name:
                raise ValueError("Unsafe archive path")
            if (item.external_attr >> 16) & 0o170000 == 0o120000:
                if source:
                    raise ValueError("Source archive symlink")
                target = archive.read(item).decode("utf-8")
                if '\\' in target or ':' in target:
                    raise ValueError('Unsafe archive symlink target')
                # macOS framework links must remain inside the package root.
                depth = len(parts) - 1
                for part in Path(target).parts:
                    depth += -1 if part == ".." else 0 if part == "." else 1
                    if depth < 1 or target.startswith("/"):
                        raise ValueError("Escaping archive symlink")
            if any(part.lower() in {".env", "config.json", ".git", ".venv", "tmp", "__pycache__"} or part.lower().startswith(('.env.', 'config.json.')) for part in parts) or re.search(r'(?i)\.(?:sqlite3?|db)(?:-.*)?$', name) or Path(name).suffix.lower() in {".key", ".pem", ".p12", ".pfx", ".log"}:
                raise ValueError(f"Forbidden archive content: {name}")
            if re.search(r"(?i)(^|/)(libvlc[^/]*\.(?:dylib|dll)|vlc\.dll)$", name):
                raise ValueError("External VLC bundled")
        bad = archive.testzip()
        if bad:
            raise ValueError(f"Corrupt archive entry: {bad}")
        if source:
            prefix = f"VLCMonitor-{version()}-source/"
            expected = {prefix + p.relative_to(ROOT).as_posix() for p in source_files()}
            if set(names) != expected:
                raise ValueError("Source archive differs from controlled whitelist")
            for p in source_files():
                if archive.read(prefix + p.relative_to(ROOT).as_posix()) != p.read_bytes():
                    raise ValueError("Source archive content mismatch")


def secret_scan() -> None:
    paths = source_files()
    # Capture scanner output: only detector names and paths may reach logs.
    result = subprocess.run(["uvx", "--from", "detect-secrets==1.5.0", "detect-secrets", "scan", "--all-files", *[str(p.relative_to(ROOT)) for p in paths]], cwd=ROOT, capture_output=True, text=True)
    if result.returncode:
        raise ValueError("Pinned secret scanner failed (raw output suppressed)")
    data = json.loads(result.stdout)
    allowed = json.loads((ROOT / '.github/secret-allowlist.json').read_text(encoding='utf-8'))
    approved = {(item['path'], item['type'], bytes(item['fingerprint_bytes']).hex()) for item in allowed}
    findings = {name: [item for item in items if (name, item['type'], item['hashed_secret']) not in approved]
                for name, items in data.get('results', {}).items()}
    findings = {name: items for name, items in findings.items() if items}
    for name, items in findings.items():
        for item in items:
            print(f"SECRET RISK: {name}: {item.get('type', 'unknown detector')}")
    if findings:
        raise ValueError("Secret scanner findings require review; no raw values printed")
    print(f"Pinned detect-secrets 1.5.0: {len(paths)} whitelisted files, no findings. Manual review still required.")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["version", "audit", "scan", "checksums", "verify-checksums", "audit-zip"])
    parser.add_argument("paths", nargs="*")
    parser.add_argument("--source", action="store_true")
    args = parser.parse_args()
    if args.command == "version": print(version())
    elif args.command == "audit":
        version()
        paths = source_files()
        print(json.dumps([p.relative_to(ROOT).as_posix() for p in paths], indent=2))
    elif args.command == "scan": secret_scan()
    elif args.command == "checksums": checksums([Path(p) for p in args.paths[1:]], Path(args.paths[0]))
    elif args.command == "verify-checksums":
        verify_checksums(Path(args.paths[0]), set(args.paths[1:]) if len(args.paths) > 1 else None)
    elif args.command == "audit-zip": audit_zip(Path(args.paths[0]), args.source)


if __name__ == "__main__":
    main()
