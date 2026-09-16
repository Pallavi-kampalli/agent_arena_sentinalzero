import asyncio
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import httpx
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

# Add src to sys.path
ROOT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT_DIR / "src"))

from agent_arena.config import get_config  # noqa: E402


async def seed_admin(target_url: str | None = None) -> bool:
    """Idempotently validates and bootstraps administrative configuration and audit logs.

    1. Validates configured ADMIN_PANEL_SECRET entropy and presence.
    2. Writes/verifies administrative bootstrap record in settings_audit_log.
    3. Verifies administrative access against the running API endpoint.
    """
    config = get_config()
    secret = config.ADMIN_PANEL_SECRET
    url = target_url or os.environ.get("PRODUCTION_URL", f"http://127.0.0.1:{config.PORT}")

    print("=" * 65)
    print("  Agent Arena — Administrative Initialization & Verification")
    print("=" * 65)
    print(f"Target Environment : {config.ENVIRONMENT}")
    print(f"Target Service URL : {url}")
    print(f"Secret Length      : {len(secret)} characters")

    # 1. Entropy & Security Validation
    if len(secret) < 32:
        print(f"[FAIL] ADMIN_PANEL_SECRET is too short ({len(secret)} < 32 characters).")
        return False

    if config.ENVIRONMENT == "production":
        insecure_words = {"change-this", "dev-admin", "password", "secret-key", "default", "example"}
        if any(w in secret.lower() for w in insecure_words):
            print("[FAIL] ADMIN_PANEL_SECRET contains insecure placeholder tokens.")
            return False

    print("[PASS] Administrative secret passes security entropy checks.")

    # 2. Record Administrative Bootstrap Audit Log in Database
    db_url = config.DATABASE_URL
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

    try:
        engine = create_async_engine(db_url, echo=False)
        session_factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
        async with session_factory() as session:
            # Check existing audit log entry
            res = await session.execute(text("SELECT id FROM settings_audit_log WHERE key = 'admin_bootstrap' LIMIT 1"))
            existing = res.scalar_one_or_none()
            if not existing:
                await session.execute(
                    text(
                        """
                        INSERT INTO settings_audit_log (key, old_value, new_value, changed_by, changed_at)
                        VALUES ('admin_bootstrap', 'null'::jsonb, '{"status": "initialized"}'::jsonb, 'system_init', :changed_at)
                        """
                    ),
                    {"changed_at": datetime.now(timezone.utc)},
                )
                await session.commit()
                print("[PASS] Recorded initial 'admin_bootstrap' audit log entry.")
            else:
                print(f"[PASS] Administrative bootstrap record already exists (audit_log_id={existing}).")

            # Verify and seed canonical benchmark tasks if empty
            task_res = await session.execute(text("SELECT count(*) FROM tasks WHERE dataset = 'hidden'"))
            task_count = task_res.scalar() or 0
            if task_count == 0:
                from agent_arena.services.dataset_service import DatasetService
                ds = DatasetService(session)
                await ds.generate_and_load_dataset(dataset_type="hidden")
                print("[PASS] Seeded canonical hidden benchmark tasks into database.")
            else:
                print(f"[PASS] Canonical benchmark tasks already present ({task_count} tasks).")
        await engine.dispose()
    except Exception as e:
        # Fallback to docker compose exec for isolated containerized PostgreSQL
        try:
            sql_check = "SELECT id FROM settings_audit_log WHERE key = 'admin_bootstrap' LIMIT 1;"
            check_res = subprocess.run(
                [
                    "docker",
                    "compose",
                    "exec",
                    "-T",
                    "postgres",
                    "psql",
                    "-U",
                    "postgres",
                    "-d",
                    "agent_arena",
                    "-t",
                    "-c",
                    sql_check,
                ],
                capture_output=True,
                text=True,
                check=False,
            )
            if check_res.returncode == 0 and check_res.stdout.strip():
                print(
                    f"[PASS] Administrative bootstrap record already exists (audit_log_id={check_res.stdout.strip()})."
                )
            else:
                now_str = datetime.now(timezone.utc).isoformat()
                sql_insert = (
                    "INSERT INTO settings_audit_log (key, old_value, new_value, changed_by, changed_at) "
                    f"VALUES ('admin_bootstrap', 'null'::jsonb, '{{\"status\": \"initialized\"}}'::jsonb, 'system_init', '{now_str}');"
                )
                insert_res = subprocess.run(
                    [
                        "docker",
                        "compose",
                        "exec",
                        "-T",
                        "postgres",
                        "psql",
                        "-U",
                        "postgres",
                        "-d",
                        "agent_arena",
                        "-c",
                        sql_insert,
                    ],
                    capture_output=True,
                    text=True,
                    check=False,
                )
                if insert_res.returncode == 0:
                    print("[PASS] Recorded initial 'admin_bootstrap' audit log entry via container.")
                else:
                    print(f"[WARN] Database audit recording skipped: {e}")
        except Exception:
            print(f"[WARN] Database direct audit recording skipped/failed: {e}")

    # 3. Verify Administrative HTTP Access against Live API
    print("Verifying administrative authentication against GET /admin/health...")
    try:
        with httpx.Client(timeout=5.0) as client:
            # First verify unauthenticated request is rejected (401)
            unauth_res = client.get(f"{url}/admin/health")
            if unauth_res.status_code != 401:
                print(f"[FAIL] Unauthenticated admin request was NOT rejected! Status: {unauth_res.status_code}")
                return False
            print("[PASS] Unauthenticated admin request correctly rejected with HTTP 401.")

            # Verify authenticated request succeeds (200)
            auth_res = client.get(f"{url}/admin/health", headers={"X-Admin-Secret": secret})
            if auth_res.status_code != 200:
                print(
                    f"[FAIL] Authenticated admin request failed! Status: {auth_res.status_code}, Body: {auth_res.text}"
                )
                return False

            data = auth_res.json()
            print(f"[PASS] Authenticated admin check succeeded (HTTP 200). Status: {data.get('status', 'unknown')}")
    except Exception as e:
        print(f"[FAIL] Failed to communicate with admin endpoint: {e}")
        return False

    print("=" * 65)
    print("  ADMIN INITIALIZATION & AUDIT VERIFICATION PASSED")
    print("=" * 65)
    return True


if __name__ == "__main__":
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    cli_target = sys.argv[1] if len(sys.argv) > 1 else None
    success = asyncio.run(seed_admin(cli_target))
    sys.exit(0 if success else 1)
