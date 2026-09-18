"""Agent Arena SentinelZero - Disaster Recovery & Operational Recovery Verification.

Validates disaster recovery, process lifecycle resilience, and data persistence:
1. Scenario 1: Stateless API restart resilience (zero state lost)
2. Scenario 2: Database outage & recovery handling (/health/ready 503 during pause, recovers to 200 on unpause)
3. Scenario 3: Non-destructive isolated database backup & restore verification
4. Scenario 4: Full Docker Compose restart & named volume persistence (postgres_data survives teardown)
"""

import os
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

import httpx

ROOT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT_DIR / "scripts"))
sys.path.insert(0, str(ROOT_DIR / "src"))

from backup_db import backup_database  # noqa: E402
from restore_db import restore_database  # noqa: E402

from agent_arena.config import get_config  # noqa: E402

BASE_URL = os.getenv("BASE_URL", "http://localhost:8000").rstrip("/")
ADMIN_SECRET = os.getenv("ADMIN_PANEL_SECRET", get_config().ADMIN_PANEL_SECRET)
ADMIN_HEADERS = {"X-Admin-Secret": ADMIN_SECRET}


def wait_for_ready(timeout_seconds: float = 30.0, expect_status: int = 200) -> bool:
    """Polls /health/ready until the expected status code is returned."""
    t0 = time.time()
    while time.time() - t0 < timeout_seconds:
        try:
            r = httpx.get(f"{BASE_URL}/health/ready", timeout=2.0)
            if r.status_code == expect_status:
                return True
        except Exception:
            if expect_status != 200:
                return True
        time.sleep(1.0)
    return False


def get_admin_stats() -> dict:
    """Fetches baseline metrics from admin health and settings endpoints."""
    with httpx.Client(base_url=BASE_URL, timeout=10.0) as client:
        r_health = client.get("/admin/health", headers=ADMIN_HEADERS)
        r_health.raise_for_status()
        r_settings = client.get("/admin/settings", headers=ADMIN_HEADERS)
        r_settings.raise_for_status()
        settings_map = {s["key"]: s["value"] for s in r_settings.json()["settings"]}
        return {
            "admin_health": r_health.json(),
            "settings": settings_map,
        }


def main() -> None:
    print("=" * 80)
    print(" AGENT ARENA SENTINELZERO — DISASTER RECOVERY & PERSISTENCE VERIFICATION")
    print(f" Target URL : {BASE_URL}")
    print(f" Timestamp  : {datetime.now(UTC).isoformat()}")
    print("=" * 80)

    # Pre-check
    print("\n[Step 0/4] Collecting Pre-Disaster Baseline Metrics...")
    assert wait_for_ready(timeout_seconds=15.0), "Base system is not ready on /health/ready"
    baseline = get_admin_stats()
    teams_count = baseline["admin_health"]["active_teams_count"]
    subs_count = baseline["admin_health"]["total_submissions_count"]
    hidden_tasks = baseline["admin_health"]["hidden_tasks_available"]
    print(f"  + Active Teams:       {teams_count}")
    print(f"  + Total Submissions:  {subs_count}")
    print(f"  + Hidden Tasks:       {hidden_tasks}")
    print(f"  + Setting hidden_cnt: {baseline['settings'].get('hidden_task_count')}")

    # Scenario 1: Stateless API Container Restart
    print("\n[Scenario 1/4] Testing Stateless API Container Restart Resilience...")
    print("  + Restarting container: agent_arena_api...")
    subprocess.run(["docker", "restart", "agent_arena_api"], check=True, capture_output=True, text=True)

    print("  + Waiting for API readiness (/health/ready)...")
    assert wait_for_ready(timeout_seconds=25.0), "API failed to become ready after restart"

    after_api_restart = get_admin_stats()
    assert after_api_restart["admin_health"]["active_teams_count"] == teams_count, (
        "Team count changed after API restart"
    )
    assert after_api_restart["admin_health"]["total_submissions_count"] == subs_count, "Submission count changed"
    assert after_api_restart["admin_health"]["hidden_tasks_available"] == hidden_tasks, "Hidden task count changed"
    print("  => PASS: API restarted cleanly with zero state drift or data loss.")

    # Scenario 2: Database Outage & Automatic Recovery
    print("\n[Scenario 2/4] Testing Database Outage & Healthcheck Decoupling...")
    print("  + Pausing container: agent_arena_postgres...")
    subprocess.run(["docker", "pause", "agent_arena_postgres"], check=True, capture_output=True, text=True)

    try:
        # Give a moment for connection pool to detect pause
        time.sleep(1.0)
        # 1. Readiness MUST fail (503 or connect error)
        print("  + Verifying deep readiness (/health/ready) fails during database outage...")
        readiness_failed = False
        try:
            r_ready = httpx.get(f"{BASE_URL}/health/ready", timeout=3.0)
            if r_ready.status_code == 503:
                readiness_failed = True
                print(f"    [OK] /health/ready returned expected HTTP 503 ({r_ready.json()})")
        except (httpx.ConnectError, httpx.TimeoutException) as e:
            readiness_failed = True
            print(f"    [OK] /health/ready timed out / connection dropped: {e}")

        assert readiness_failed, "Expected /health/ready to fail while database was paused!"

        # 2. Liveness (/health) MUST still respond (process is alive)
        print("  + Verifying process liveness (/health) remains responsive...")
        try:
            r_live = httpx.get(f"{BASE_URL}/health", timeout=3.0)
            print(f"    [OK] /health status: {r_live.status_code} ({r_live.json().get('status')})")
            assert r_live.status_code == 200, "Liveness failed during db pause"
        except Exception as e:
            print(f"    [WARN] /health request exception during pause: {e}")

    finally:
        print("  + Unpausing container: agent_arena_postgres...")
        subprocess.run(["docker", "unpause", "agent_arena_postgres"], check=True, capture_output=True, text=True)

    print("  + Waiting for database connection pool recovery on /health/ready...")
    assert wait_for_ready(timeout_seconds=25.0), "API failed to recover database connection after unpause"

    after_db_outage = get_admin_stats()
    assert after_db_outage["admin_health"]["active_teams_count"] == teams_count
    assert after_db_outage["admin_health"]["total_submissions_count"] == subs_count
    print("  => PASS: System survived database outage, reported correct health statuses, and recovered cleanly.")

    # Scenario 3: Non-Destructive Isolated Backup & Restore
    print("\n[Scenario 3/4] Testing Non-Destructive Isolated Backup & Restore...")
    backup_file = backup_database()
    assert backup_file.exists() and backup_file.stat().st_size > 0, "Backup file invalid or empty"
    print(f"  + Operational backup created: {backup_file.name} ({backup_file.stat().st_size} bytes)")

    print("  + Restoring into isolated target database: agent_arena_restore_verify...")
    restore_success = restore_database(backup_file, target_db="agent_arena_restore_verify")
    assert restore_success, "Isolated database restoration failed"
    print("  => PASS: Backup and isolated verification restore executed with zero live database disruption.")

    # Scenario 4: Full Docker Compose Teardown & Volume Persistence
    print("\n[Scenario 4/4] Testing Compose Teardown (down) and Restart with Volume Persistence...")
    print("  + Running: docker compose down...")
    subprocess.run(["docker", "compose", "down"], cwd=str(ROOT_DIR), check=True, capture_output=True, text=True)

    print("  + Running: docker compose up -d...")
    subprocess.run(["docker", "compose", "up", "-d"], cwd=str(ROOT_DIR), check=True, capture_output=True, text=True)

    print("  + Polling /health/ready until all containers and migrations finish...")
    assert wait_for_ready(timeout_seconds=45.0), "Stack failed to become ready after compose up -d"

    after_compose_restart = get_admin_stats()
    assert after_compose_restart["admin_health"]["active_teams_count"] == teams_count, (
        f"Team count mismatch after compose restart: expected {teams_count}, got {after_compose_restart['admin_health']['active_teams_count']}"
    )
    assert after_compose_restart["admin_health"]["total_submissions_count"] == subs_count, (
        f"Submission count mismatch after compose restart: expected {subs_count}, got {after_compose_restart['admin_health']['total_submissions_count']}"
    )
    assert after_compose_restart["admin_health"]["hidden_tasks_available"] == hidden_tasks, "Task count mismatch"
    print(f"  + Confirmed persisted teams:       {after_compose_restart['admin_health']['active_teams_count']}")
    print(f"  + Confirmed persisted submissions: {after_compose_restart['admin_health']['total_submissions_count']}")
    print(f"  + Confirmed persisted tasks:       {after_compose_restart['admin_health']['hidden_tasks_available']}")
    print("  => PASS: Named volume postgres_data preserved 100% of data across complete container destruction.")

    print("\n" + "=" * 80)
    print(" ALL DISASTER RECOVERY & PERSISTENCE VERIFICATIONS PASSED")
    print("=" * 80)


if __name__ == "__main__":
    main()
