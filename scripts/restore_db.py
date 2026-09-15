import argparse
import subprocess
import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
BACKUPS_DIR = ROOT_DIR / "backups"


def get_latest_backup() -> Path:
    """Finds the most recent SQL backup file in the backups directory."""
    if not BACKUPS_DIR.exists():
        print("[FAIL] Backups directory does not exist.")
        sys.exit(1)

    backups = sorted(BACKUPS_DIR.glob("agent_arena_backup_*.sql"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not backups:
        print("[FAIL] No backup files found in backups directory.")
        sys.exit(1)

    return backups[0]


def restore_database(backup_path: Path, target_db: str = "agent_arena_restore_verify", force: bool = False) -> bool:
    """Restores database from backup archive. Defaults to isolated verification database."""
    print("=" * 65)
    print("  Agent Arena — Operational Database Restoration")
    print("=" * 65)
    print(f"Backup Source : {backup_path}")
    print(f"Target DB     : {target_db}")
    print(f"Isolated Mode : {'YES' if target_db != 'agent_arena' else 'NO (PRODUCTION IN-PLACE)'}")

    if not backup_path.exists():
        print(f"[FAIL] Backup file does not exist: {backup_path}")
        return False

    if target_db == "agent_arena" and not force:
        print("[FAIL] Refusing to restore to live production database without --force flag!")
        return False

    # 1. Prepare target database via docker compose exec
    if target_db != "agent_arena":
        print(f"Preparing isolated database '{target_db}'...")
        drop_cmd = [
            "docker",
            "compose",
            "exec",
            "-T",
            "postgres",
            "psql",
            "-U",
            "postgres",
            "-c",
            f"DROP DATABASE IF EXISTS {target_db};",
        ]
        create_cmd = [
            "docker",
            "compose",
            "exec",
            "-T",
            "postgres",
            "psql",
            "-U",
            "postgres",
            "-c",
            f"CREATE DATABASE {target_db};",
        ]
        try:
            subprocess.run(drop_cmd, cwd=str(ROOT_DIR), check=True, capture_output=True, text=True)
            subprocess.run(create_cmd, cwd=str(ROOT_DIR), check=True, capture_output=True, text=True)
            print(f"[PASS] Created fresh isolated database '{target_db}'.")
        except subprocess.CalledProcessError as e:
            print(f"[FAIL] Failed to create isolated database: {e.stderr}")
            return False

    # 2. Feed backup SQL into target database
    print(f"Streaming SQL restore into '{target_db}'...")
    restore_cmd = [
        "docker",
        "compose",
        "exec",
        "-T",
        "postgres",
        "psql",
        "-U",
        "postgres",
        "-d",
        target_db,
    ]

    try:
        with open(backup_path, "r", encoding="utf-8", errors="ignore") as f:
            subprocess.run(
                restore_cmd,
                cwd=str(ROOT_DIR),
                stdin=f,
                capture_output=True,
                text=True,
                check=True,
            )
        print(f"[PASS] SQL dump streamed successfully into '{target_db}'.")
    except subprocess.CalledProcessError as e:
        print(f"[FAIL] Restoration failed with exit code {e.returncode}: {e.stderr}")
        return False

    # 3. Validate restored schema and row counts
    print("Verifying schema and tables in restored database...")
    verify_cmd = [
        "docker",
        "compose",
        "exec",
        "-T",
        "postgres",
        "psql",
        "-U",
        "postgres",
        "-d",
        target_db,
        "-t",
        "-A",
        "-c",
        """
        SELECT
            (SELECT count(*) FROM teams) as teams_count,
            (SELECT count(*) FROM tasks) as tasks_count,
            (SELECT count(*) FROM settings) as settings_count,
            (SELECT count(*) FROM settings_audit_log) as audit_count;
        """,
    ]

    try:
        res = subprocess.run(verify_cmd, cwd=str(ROOT_DIR), capture_output=True, text=True, check=True)
        counts = res.stdout.strip().split("|")
        if len(counts) == 4:
            t_cnt, task_cnt, s_cnt, a_cnt = counts
            print("[PASS] Restored Database Statistics:")
            print(f"       Teams              : {t_cnt}")
            print(f"       Tasks              : {task_cnt}")
            print(f"       Settings           : {s_cnt}")
            print(f"       Audit Logs         : {a_cnt}")
        else:
            print(f"[WARN] Table query output: {res.stdout.strip()}")
    except subprocess.CalledProcessError as e:
        print(f"[FAIL] Failed to query restored tables: {e.stderr}")
        return False

    print("=" * 65)
    print(f"  RESTORATION VERIFICATION PASSED ({target_db})")
    print("=" * 65)
    return True


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Agent Arena Database Restoration Utility")
    parser.add_argument("--backup", type=str, default=None, help="Path to backup SQL file (defaults to latest)")
    parser.add_argument("--target", type=str, default="agent_arena_restore_verify", help="Target database name")
    parser.add_argument("--force", action="store_true", help="Force restoration into live production database")
    args = parser.parse_args()

    source = Path(args.backup) if args.backup else get_latest_backup()
    success = restore_database(source, target_db=args.target, force=args.force)
    sys.exit(0 if success else 1)
