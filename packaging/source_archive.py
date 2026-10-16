"""Create the controlled source attachment once, independently of native builds."""
from pathlib import Path
import sys
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
from delivery import ROOT, audit_zip, checksums, source_files, version

value = version()
output = ROOT / f"dist/VLCMonitor-{value}-source.zip"
output.parent.mkdir(exist_ok=True)
with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
    for path in source_files():
        info = zipfile.ZipInfo(f"VLCMonitor-{value}-source/{path.relative_to(ROOT).as_posix()}")
        info.date_time = (2026, 1, 1, 0, 0, 0)
        info.external_attr = (0o100755 if path.suffix == ".sh" else 0o100644) << 16
        info.compress_type = zipfile.ZIP_DEFLATED
        archive.writestr(info, path.read_bytes())
audit_zip(output, source=True)
checksums([output], ROOT / f"dist/SHA256SUMS-{value}-source.txt")
print(output.name)
