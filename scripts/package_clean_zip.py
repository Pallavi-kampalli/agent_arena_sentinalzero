"""Packages SentinelZero starter kit cleanly into distribution zip files."""

import os
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STARTER_KIT_DIR = ROOT / "starter-kit"

OUTPUT_ZIPS = [
    ROOT / "starter-kit.zip",
    ROOT / "sentinel_zero_starter_kit.zip",
    ROOT.parent / "starter_kits_packaged" / "sentinel_zero_starter_kit.zip",
]

EXCLUDE_EXTS = {".pyc", ".pyo", ".db", ".log"}
EXCLUDE_NAMES = {"mock_arena.db", ".DS_Store", "Thumbs.db"}
EXCLUDE_DIRS = {"__pycache__", ".venv", ".pytest_cache"}


def should_include(file_path: Path) -> bool:
    if file_path.suffix in EXCLUDE_EXTS:
        return False
    if file_path.name in EXCLUDE_NAMES:
        return False
    for part in file_path.parts:
        if part in EXCLUDE_DIRS:
            return False
    return True


def create_zip(zip_path: Path):
    zip_path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as z:
        for root, dirs, files in os.walk(STARTER_KIT_DIR):
            # prune excluded dirs
            dirs[:] = [d for d in dirs if d not in EXCLUDE_DIRS]
            for file in sorted(files):
                file_path = Path(root) / file
                if not should_include(file_path):
                    continue
                rel_path = file_path.relative_to(STARTER_KIT_DIR)
                arcname = f"starter-kit/{rel_path.as_posix()}"
                z.write(file_path, arcname)
    print(f"Created: {zip_path} ({zip_path.stat().st_size:,} bytes)")


def main():
    for zpath in OUTPUT_ZIPS:
        create_zip(zpath)


if __name__ == "__main__":
    main()
