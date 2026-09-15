import datetime
import subprocess
import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
BACKUPS_DIR = ROOT_DIR / "backups"


def backup_database(output_path: Path | None = None) -> Path:
    """Performs an operational backup of PostgreSQL via docker compose exec."""
    BACKUPS_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%d_%H%M%S")
    target_file = output_path or (BACKUPS_DIR / f"agent_arena_backup_{timestamp}.sql")

    print("=" * 65)
    print("  Agent Arena — Operational PostgreSQL Backup")
    print("=" * 65)
    print(f"Timestamp   : {timestamp} UTC")
    print(f"Target File : {target_file}")

    cmd = [
        "docker",
        "compose",
        "exec",
        "-T",
        "postgres",
        "pg_dump",
        "-U",
        "postgres",
        "-d",
        "agent_arena",
        "--clean",
        "--if-exists",
    ]

    print("Executing pg_dump via postgres container...")
    try:
        with open(target_file, "w", encoding="utf-8") as f:
            subprocess.run(
                cmd,
                cwd=str(ROOT_DIR),
                stdout=f,
                stderr=subprocess.PIPE,
                text=True,
                check=True,
            )
    except subprocess.CalledProcessError as e:
        print(f"[FAIL] pg_dump failed with exit code {e.returncode}: {e.stderr}")
        if target_file.exists():
            target_file.unlink()
        sys.exit(1)
    except Exception as e:
        print(f"[FAIL] Unexpected error during backup: {e}")
        if target_file.exists():
            target_file.unlink()
        sys.exit(1)

    # Validate file existence and size
    if not target_file.exists():
        print("[FAIL] Backup file was not created.")
        sys.exit(1)

    size = target_file.stat().st_size
    if size == 0:
        print("[FAIL] Backup file is empty (0 bytes).")
        target_file.unlink()
        sys.exit(1)

    # Validate header
    with open(target_file, "r", encoding="utf-8", errors="ignore") as f:
        header = f.read(500)
        if "PostgreSQL database dump" not in header:
            print("[FAIL] Backup file header is invalid (missing 'PostgreSQL database dump').")
            sys.exit(1)

    print(f"[PASS] Backup created successfully: {target_file} ({size:,} bytes)")
    print("=" * 65)
    return target_file


if __name__ == "__main__":
    backup_database()
