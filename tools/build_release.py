"""Build the release archive the auto-updater consumes.

    python tools/build_release.py [--out dist]

Produces dist/w4boc-battery-monitor-v<VERSION>.zip (everything under a single
top-level folder) and dist/SHA256SUMS.txt. The same script runs in the GitHub
release workflow, so a local build is byte-for-byte what gets published.
"""
from __future__ import annotations

import argparse
import hashlib
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
NAME = "w4boc-battery-monitor"

INCLUDE_FILES = [
    "VERSION", "main.py", "launcher.py", "run.bat", "manage.py",
    "requirements.txt", "config.example.toml", "secrets.toml.example",
    "README.md", "CHANGELOG.md", "LICENSE",
]
INCLUDE_DIRS = ["w4boc", "tools", "docs"]
EXCLUDE_SUFFIXES = (".pyc", ".pdf", ".db", ".log")
EXCLUDE_DIRS = {"__pycache__", ".pytest_cache"}


def iter_release_files():
    for f in INCLUDE_FILES:
        p = ROOT / f
        if p.exists():
            yield p
    for d in INCLUDE_DIRS:
        for p in sorted((ROOT / d).rglob("*")):
            if not p.is_file():
                continue
            if any(part in EXCLUDE_DIRS for part in p.relative_to(ROOT).parts):
                continue
            if p.name.endswith(EXCLUDE_SUFFIXES):
                continue
            yield p


def build(out_dir: Path) -> tuple[Path, Path]:
    version = (ROOT / "VERSION").read_text(encoding="utf-8").strip()
    out_dir.mkdir(parents=True, exist_ok=True)
    zip_path = out_dir / f"{NAME}-v{version}.zip"
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as z:
        for p in iter_release_files():
            arc = f"{NAME}/{p.relative_to(ROOT).as_posix()}"
            z.write(p, arc)
    digest = hashlib.sha256(zip_path.read_bytes()).hexdigest()
    sums = out_dir / "SHA256SUMS.txt"
    sums.write_text(f"{digest}  {zip_path.name}\n", encoding="utf-8")
    return zip_path, sums


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(ROOT / "dist"))
    args = ap.parse_args()
    zip_path, sums = build(Path(args.out))
    n = sum(1 for _ in iter_release_files())
    print(f"built {zip_path} ({n} files, {zip_path.stat().st_size // 1024} KB)")
    print(sums.read_text().strip())
    return 0


if __name__ == "__main__":
    sys.exit(main())
