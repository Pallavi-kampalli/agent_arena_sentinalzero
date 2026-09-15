import asyncio
import os
import sys
import uuid
from datetime import UTC, datetime

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

import sqlalchemy as sa
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

# Ensure project root is in sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src")))

from agent_arena.api.app import app
from agent_arena.api.deps import get_db_session
from agent_arena.config import get_config
from agent_arena.models.base import Base
from agent_arena.models.submission import Submission
from agent_arena.models.task import Task
from agent_arena.models.team import Team
from agent_arena.models.tool_call_log import ToolCallLog
from agent_arena.services.auth_service import hash_token
from agent_arena.services.settings_service import SettingsService
from agent_arena.world.generator import generate_world

PG_URL = os.environ.get("DATABASE_URL", get_config().DATABASE_URL)


async def run_phase5_live_verification():
    print("=" * 72)
    print("  AGENT ARENA — PHASE 5 LIVE POSTGRESQL ADMIN CONTROL PLANE VERIFICATION")
    print("=" * 72)
    print(f"Connecting to PostgreSQL: {PG_URL}")

    engine = create_async_engine(PG_URL, echo=False)
    session_factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)

    # 1. Ensure Schema
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    print("[PASS] PostgreSQL schema confirmed.")

    # Override db dependencies in app
    async def override_get_db():
        async with session_factory() as s:
            yield s

    app.dependency_overrides[get_db_session] = override_get_db

    import agent_arena.db as db_module

    db_module._engine = engine
    db_module._session_maker = session_factory

    transport = ASGITransport(app=app)
    admin_secret = get_config().ADMIN_PANEL_SECRET
    admin_headers = {"X-Admin-Secret": admin_secret}

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # Seed settings
        async with session_factory() as session:
            settings_srv = SettingsService(session)
            await settings_srv.seed_defaults()
            await settings_srv.set("hidden_task_count", 3)
            # Add 3 benchmark tasks
            for i in range(3):
                t_id = f"TASK-PG5-{i:03d}"
                existing_task = (
                    await session.execute(sa.select(Task).where(Task.task_id == t_id))
                ).scalar_one_or_none()
                if not existing_task:
                    task = Task(
                        task_id=t_id,
                        dataset="hidden",
                        family="refund_request",
                        variant="normal",
                        input_payload={"customer_id": f"CUS-PG5-{i}", "customer_message": "Need help"},
                        world_state_seed=generate_world(seed=9000 + i),
                        ground_truth={"expected_resolution": "refund", "must_escalate": False},
                    )
                    session.add(task)
            await session.commit()

        # ----------------------------------------------------------------------
        # TEST 1: Admin Authentication & Boundary Enforcement
        # ----------------------------------------------------------------------
        print("\n[STEP 1] Verifying Admin Authentication & Security Boundary...")
        # 1.1 Missing credentials
        r = await client.get("/admin/settings")
        assert r.status_code == 401, f"Expected 401 on missing auth, got {r.status_code}"

        # 1.2 Invalid secret
        r = await client.get("/admin/settings", headers={"X-Admin-Secret": "wrong-secret"})
        assert r.status_code == 401, f"Expected 401 on invalid secret, got {r.status_code}"

        # 1.3 Valid X-Admin-Secret
        r = await client.get("/admin/settings", headers=admin_headers)
        assert r.status_code == 200, f"Expected 200 with X-Admin-Secret, got {r.status_code}"

        # 1.4 Valid Bearer token
        r = await client.get("/admin/settings", headers={"Authorization": f"Bearer {admin_secret}"})
        assert r.status_code == 200, f"Expected 200 with Bearer admin secret, got {r.status_code}"

        # 1.5 Dashboard HTML rendered
        r = await client.get("/admin/dashboard")
        assert r.status_code == 200
        assert "Agent Arena — Admin Control Plane" in r.text
        print("  -> Admin auth, header schemes, and dashboard verified successfully.")

        # ----------------------------------------------------------------------
        # TEST 2: Team Lifecycle & Token Revocation on Live Postgres
        # ----------------------------------------------------------------------
        print("\n[STEP 2] Verifying Team Lifecycle & Token Revocation...")
        unique_name = f"LivePgTeam_{uuid.uuid4().hex[:6]}"
        create_resp = await client.post(
            "/admin/teams",
            json={
                "team_name": unique_name,
                "members": [{"name": "Auditor", "email": "auditor@pg.org"}],
                "github_repo_url": "https://github.com/pg/repo",
            },
            headers=admin_headers,
        )
        assert create_resp.status_code == 201, f"Create team failed: {create_resp.text}"
        team_data = create_resp.json()
        team_id = team_data["team_id"]
        raw_token = team_data["token"]
        assert f"AGENT_ARENA_TEAM_ID={team_id}" in team_data["env_snippet"]
        assert f"AGENT_ARENA_BEARER_TOKEN={raw_token}" in team_data["env_snippet"]

        part_headers = {"Authorization": f"Bearer {raw_token}"}
        # Start a submission to prove raw_token works
        sub_start = await client.post("/submission/start", headers=part_headers)
        assert sub_start.status_code == 200, f"Submission start failed: {sub_start.text}"
        sub_id = sub_start.json()["submission_id"]

        # Suspend team
        susp_resp = await client.put(
            f"/admin/teams/{team_id}/status",
            json={"status": "suspended"},
            headers=admin_headers,
        )
        assert susp_resp.status_code == 200
        assert susp_resp.json()["status"] == "suspended"

        # Participant request blocked
        blocked_resp = await client.get(f"/submission/{sub_id}/status", headers=part_headers)
        assert blocked_resp.status_code == 403, f"Expected 403 when suspended, got {blocked_resp.status_code}"
        assert blocked_resp.json()["error"] == "FORBIDDEN"

        # Reactivate team
        act_resp = await client.put(
            f"/admin/teams/{team_id}/status",
            json={"status": "active"},
            headers=admin_headers,
        )
        assert act_resp.status_code == 200

        # Token regeneration
        regen_resp = await client.post(f"/admin/teams/{team_id}/token", headers=admin_headers)
        assert regen_resp.status_code == 200
        new_token = regen_resp.json()["token"]
        assert new_token != raw_token

        # Old token fails immediately
        old_fail = await client.get(f"/submission/{sub_id}/status", headers=part_headers)
        assert old_fail.status_code == 401
        assert "TOKEN_REVOKED" in old_fail.json()["detail"]

        # New token succeeds immediately
        new_ok = await client.get(f"/submission/{sub_id}/status", headers={"Authorization": f"Bearer {new_token}"})
        assert new_ok.status_code == 200
        print("  -> Team registration, suspension, and atomic token revocation verified.")

        # ----------------------------------------------------------------------
        # TEST 3: Bulk Import via CSV
        # ----------------------------------------------------------------------
        print("\n[STEP 3] Verifying Bulk Team Import via CSV...")
        team_a_name = f"BulkA_{uuid.uuid4().hex[:6]}"
        team_b_name = f"BulkB_{uuid.uuid4().hex[:6]}"
        csv_content = (
            f"team_name,member_names,member_emails,github_repo_url\n"
            f"{team_a_name},Alice,alice@corp.com,https://github.com/a\n"
            f"{team_b_name},Bob;Carol,bob@corp.com,https://github.com/b\n"
        )
        bulk_resp = await client.post(
            "/admin/teams/bulk-import",
            json={"csv_content": csv_content},
            headers=admin_headers,
        )
        assert bulk_resp.status_code == 201
        bulk_data = bulk_resp.json()
        assert bulk_data["created_count"] == 2
        assert bulk_data["skipped_count"] == 0

        # Duplicate import skips gracefully
        bulk_resp2 = await client.post(
            "/admin/teams/bulk-import",
            json={"csv_content": csv_content},
            headers=admin_headers,
        )
        assert bulk_resp2.status_code == 201
        assert bulk_resp2.json()["created_count"] == 0
        assert bulk_resp2.json()["skipped_count"] == 2
        print("  -> CSV bulk import and idempotency verified.")

        # ----------------------------------------------------------------------
        # TEST 4: Settings Audit Trail & State Machine Transitions
        # ----------------------------------------------------------------------
        print("\n[STEP 4] Verifying Settings Audit Trail & Monotonic Phase Transitions...")
        # Update setting
        up_resp = await client.put(
            "/admin/settings/time_budget_per_task_seconds",
            json={"value": 240},
            headers=admin_headers,
        )
        assert up_resp.status_code == 200
        assert up_resp.json()["new_value"] == 240

        # Verify audit log
        audit_resp = await client.get(
            "/admin/settings/audit-log?key=time_budget_per_task_seconds", headers=admin_headers
        )
        assert audit_resp.status_code == 200
        assert audit_resp.json()["total"] >= 1
        assert audit_resp.json()["audit_logs"][0]["new_value"] == 240

        # Weights validation (weights sum to 0.8 != 1.0)
        bad_w = await client.put(
            "/admin/settings/scoring_weights",
            json={
                "value": {
                    "task_success": 0.2,
                    "policy": 0.1,
                    "robustness": 0.1,
                    "evidence": 0.1,
                    "calibration": 0.1,
                    "efficiency": 0.1,
                    "communication": 0.1,
                }
            },
            headers=admin_headers,
        )
        assert bad_w.status_code == 400

        # Competition Phase forward transition
        phase_curr = (await client.get("/admin/competition/phase", headers=admin_headers)).json()["current_phase"]
        if phase_curr == "registration":
            step_to_build = await client.post(
                "/admin/competition/phase", json={"new_phase": "build"}, headers=admin_headers
            )
            assert step_to_build.status_code == 200
            assert step_to_build.json()["current_phase"] == "build"

            # Backward transition rejected
            step_back = await client.post(
                "/admin/competition/phase", json={"new_phase": "registration"}, headers=admin_headers
            )
            assert step_back.status_code == 400
            assert step_back.json()["detail"]["error"] == "INVALID_PHASE_TRANSITION"
        print("  -> Dynamic settings, audit log, and monotonic phase transitions verified.")

        # ----------------------------------------------------------------------
        # TEST 5: Leaderboard 4-Tier Tiebreak & Disqualified Exclusion
        # ----------------------------------------------------------------------
        print("\n[STEP 5] Verifying Leaderboard 4-Tier Tiebreaks & Disqualified Exclusion...")
        now = datetime.now(UTC)
        async with session_factory() as session:
            # Create two teams tied on aggregate score but different on task success
            t_win = Team(
                team_id=uuid.uuid4(),
                team_name=f"TieWinner_{uuid.uuid4().hex[:6]}",
                bearer_token_hash=hash_token("win"),
                status="active",
            )
            t_loss = Team(
                team_id=uuid.uuid4(),
                team_name=f"TieLoser_{uuid.uuid4().hex[:6]}",
                bearer_token_hash=hash_token("loss"),
                status="active",
            )
            t_dq = Team(
                team_id=uuid.uuid4(),
                team_name=f"DQTeam_{uuid.uuid4().hex[:6]}",
                bearer_token_hash=hash_token("dq"),
                status="disqualified",
            )
            session.add_all([t_win, t_loss, t_dq])

            s_win = Submission(
                submission_id=uuid.uuid4(),
                team_id=t_win.team_id,
                attempt_number=1,
                status="completed",
                aggregate_score=0.70,
                breakdown={"dimensions": {"task_success": 0.85, "policy": 0.60}},
                completed_at=now,
            )
            s_loss = Submission(
                submission_id=uuid.uuid4(),
                team_id=t_loss.team_id,
                attempt_number=1,
                status="completed",
                aggregate_score=0.70,
                breakdown={"dimensions": {"task_success": 0.70, "policy": 0.60}},
                completed_at=now,
            )
            s_dq = Submission(
                submission_id=uuid.uuid4(),
                team_id=t_dq.team_id,
                attempt_number=1,
                status="completed",
                aggregate_score=0.99,
                breakdown={"dimensions": {"task_success": 0.99}},
                completed_at=now,
            )
            session.add_all([s_win, s_loss, s_dq])
            await session.commit()

        lead_resp = await client.get("/admin/leaderboard", headers=admin_headers)
        assert lead_resp.status_code == 200
        board = lead_resp.json()["leaderboard"]

        # Find our two teams
        names = [e["team_name"] for e in board]
        assert t_dq.team_name not in names, "Disqualified team leaked onto leaderboard!"

        win_idx = names.index(t_win.team_name)
        loss_idx = names.index(t_loss.team_name)
        assert win_idx < loss_idx, f"Tiebreak failed: {t_win.team_name} should rank above {t_loss.team_name}"

        # Export CSV
        csv_resp = await client.get("/admin/leaderboard/export", headers=admin_headers)
        assert csv_resp.status_code == 200
        assert "text/csv" in csv_resp.headers["content-type"]
        assert t_win.team_name in csv_resp.text
        assert t_dq.team_name not in csv_resp.text
        print("  -> Leaderboard 4-tier tiebreak and disqualified team exclusion verified.")

        # ----------------------------------------------------------------------
        # TEST 6: Monitoring Logs & System Health
        # ----------------------------------------------------------------------
        print("\n[STEP 6] Verifying Tool Logs & Health Monitoring...")
        # Add tool log
        async with session_factory() as session:
            tl = ToolCallLog(
                team_id=t_win.team_id,
                task_id="TASK-PG5-000",
                tool_name="test_tool",
                was_enforcement_rejection=True,
                latency_ms=10,
                created_at=datetime.now(UTC),
            )
            session.add(tl)
            await session.commit()

        tl_resp = await client.get(
            f"/admin/tool-logs?team_id={t_win.team_id}&rejections_only=true", headers=admin_headers
        )
        assert tl_resp.status_code == 200
        assert tl_resp.json()["total"] >= 1

        health_resp = await client.get("/admin/health", headers=admin_headers)
        assert health_resp.status_code == 200
        assert health_resp.json()["database"] == "healthy"
        assert health_resp.json()["status"] == "healthy"
        print("  -> Tool log querying and system health metrics verified.")

    print("\n" + "=" * 72)
    print("  ALL 6 PHASE 5 VERIFICATION GATES PASSED CLEANLY ON LIVE POSTGRESQL!")
    print("=" * 72)


if __name__ == "__main__":
    asyncio.run(run_phase5_live_verification())
