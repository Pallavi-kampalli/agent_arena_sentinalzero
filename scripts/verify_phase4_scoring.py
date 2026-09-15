import asyncio
import os
import sys
import uuid

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
from agent_arena.models.task_assignment import TaskAssignment
from agent_arena.models.team import Team
from agent_arena.scoring.service import ScoringService
from agent_arena.services.auth_service import create_bearer_token, hash_token
from agent_arena.services.settings_service import SettingsService

PG_URL = os.environ.get(
    "DATABASE_URL",
    get_config().DATABASE_URL,
)


async def verify_phase4_postgres():
    print("=" * 64)
    print("  PHASE 4 LIVE POSTGRESQL & SCORING PIPELINE VERIFICATION")
    print("=" * 64)
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

    # Override db module
    import agent_arena.db as db_module

    db_module._engine = engine
    db_module._session_maker = session_factory

    async with session_factory() as session:
        # Clean up any previous test tasks
        await session.execute(sa.delete(TaskAssignment).where(TaskAssignment.task_id.like("TASK-00-PG-SCORING-%")))
        await session.execute(sa.delete(Task).where(Task.task_id.like("TASK-00-PG-SCORING-%")))
        await session.execute(sa.delete(TaskAssignment).where(TaskAssignment.task_id.like("TASK-PG-SCORING-%")))
        await session.execute(sa.delete(Task).where(Task.task_id.like("TASK-PG-SCORING-%")))
        await session.commit()

        settings_service = SettingsService(session)
        await settings_service.seed_defaults()
        await settings_service.set("hidden_task_count", 2)
        await settings_service.set("competition_phase", "build")
        await session.commit()

        # Seed Team
        team_id = uuid.uuid4()
        token = create_bearer_token(team_id, token_version=1)
        team = Team(
            team_id=team_id,
            team_name=f"Phase4VerifyTeam_{team_id.hex[:6]}",
            bearer_token_hash=hash_token(token),
            token_version=1,
            status="active",
        )
        session.add(team)

        # Seed 2 hidden tasks in PostgreSQL with prefix 'TASK-00-' so they sort first
        t1 = Task(
            task_id="TASK-00-PG-SCORING-001",
            dataset="hidden",
            family="refund_request",
            variant="normal",
            input_payload={"customer_id": "CUS-PG-9001", "customer_message": "Please refund TXN-PG-9001."},
            world_state_seed={
                "current_date": "2026-09-15T00:00:00Z",
                "customers": [{"id": "CUS-PG-9001", "name": "Eve"}],
                "transactions": [
                    {
                        "id": "TXN-PG-9001",
                        "customer_id": "CUS-PG-9001",
                        "amount": 150.0,
                        "refund_status": "completed",
                        "refunded_amount": 0.0,
                        "date": "2026-09-10T00:00:00Z",
                    }
                ],
                "policies": [
                    {
                        "id": "DOC-1001",
                        "category": "refund",
                        "updated_at": "2026-01-01T00:00:00Z",
                        "rules": {"refund_window_days": 30},
                    }
                ],
            },
            ground_truth={
                "expected_resolution": "refund",
                "must_escalate": False,
                "required_evidence": ["TXN-PG-9001", "DOC-1001"],
                "expected_action": {
                    "tool": "issue_refund",
                    "params": {"transaction_id": "TXN-PG-9001", "amount": 150.0, "reason": "verified refund"},
                },
                "expected_end_state": {
                    "transactions": [{"id": "TXN-PG-9001", "refund_status": "refunded", "refunded_amount": 150.0}],
                },
            },
        )

        t2 = Task(
            task_id="TASK-00-PG-SCORING-002",
            dataset="hidden",
            family="subscription_cancellation",
            variant="normal",
            input_payload={"customer_id": "CUS-PG-9002", "customer_message": "Cancel subscription SUB-PG-9002."},
            world_state_seed={
                "current_date": "2026-09-15T00:00:00Z",
                "customers": [{"id": "CUS-PG-9002", "name": "Frank"}],
                "subscriptions": [
                    {
                        "id": "SUB-PG-9002",
                        "customer_id": "CUS-PG-9002",
                        "status": "active",
                        "lock_in_until": "2026-08-01T00:00:00Z",
                        "has_approved_exception": False,
                    }
                ],
                "policies": [{"id": "DOC-1003", "category": "subscription", "updated_at": "2026-01-01T00:00:00Z"}],
            },
            ground_truth={
                "expected_resolution": "refund",
                "must_escalate": False,
                "required_evidence": ["SUB-PG-9002", "DOC-1003"],
                "expected_action": {
                    "tool": "cancel_subscription",
                    "params": {"customer_id": "CUS-PG-9002", "subscription_id": "SUB-PG-9002"},
                },
                "expected_end_state": {
                    "subscriptions": [{"id": "SUB-PG-9002", "status": "cancelled"}],
                },
            },
        )

        session.add_all([t1, t2])
        await session.commit()
    print("[PASS] Seeded team and 2 hidden tasks in PostgreSQL.")

    auth_headers = {"Authorization": f"Bearer {token}"}

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        # Step 1: Start submission
        sub_resp = await ac.post("/submission/start", headers=auth_headers)
        assert sub_resp.status_code == 200, f"Failed: {sub_resp.text}"
        sub_id = uuid.UUID(sub_resp.json()["submission_id"])
        print(f"[PASS] POST /submission/start -> 200 OK (submission_id={sub_id})")

        # Step 2: Task 1 Lifecycle
        t1_start = await ac.post("/task/start", headers=auth_headers)
        assert t1_start.status_code == 200, f"Failed: {t1_start.text}"
        t1_data = t1_start.json()
        assert t1_data["task_id"] == "TASK-00-PG-SCORING-001"
        print(f"[PASS] POST /task/start -> Task 1 assigned ({t1_data['task_id']}).")

        await ac.post("/tools/get_document", json={"document_id": "DOC-1001"}, headers=auth_headers)
        await ac.post(
            "/tools/issue_refund",
            json={"transaction_id": "TXN-PG-9001", "amount": 150.0, "reason": "verified refund"},
            headers=auth_headers,
        )
        t1_sub = await ac.post(
            "/task/submit",
            json={
                "task_id": "TASK-00-PG-SCORING-001",
                "case_classification": {"category": "billing", "issue": "refund", "severity": "medium"},
                "decision": {"resolution": "refund", "escalation_required": False},
                "evidence": ["TXN-PG-9001", "DOC-1001"],
                "uncertainties": [],
                "customer_response": "Hello Eve, your refund for transaction TXN-PG-9001 has been processed. Best regards.",
                "confidence": 0.95,
            },
            headers=auth_headers,
        )
        assert t1_sub.status_code == 200, f"Failed: {t1_sub.text}"
        print("[PASS] POST /task/submit -> Task 1 submitted.")

        # Step 3: Task 2 Lifecycle
        t2_start = await ac.post("/task/start", headers=auth_headers)
        assert t2_start.status_code == 200, f"Failed: {t2_start.text}"
        t2_data = t2_start.json()
        assert t2_data["task_id"] == "TASK-00-PG-SCORING-002"
        print(f"[PASS] POST /task/start -> Task 2 assigned ({t2_data['task_id']}).")

        await ac.post("/tools/get_document", json={"document_id": "DOC-1003"}, headers=auth_headers)
        await ac.post("/tools/get_subscription", json={"customer_id": "CUS-PG-9002"}, headers=auth_headers)
        await ac.post(
            "/tools/cancel_subscription",
            json={"customer_id": "CUS-PG-9002", "subscription_id": "SUB-PG-9002"},
            headers=auth_headers,
        )
        t2_sub = await ac.post(
            "/task/submit",
            json={
                "task_id": "TASK-00-PG-SCORING-002",
                "case_classification": {"category": "subscription", "issue": "cancellation", "severity": "medium"},
                "decision": {"resolution": "refund", "escalation_required": False},
                "evidence": ["SUB-PG-9002", "DOC-1003"],
                "uncertainties": [],
                "customer_response": "Hello Frank, your subscription has been cancelled per policy. Best regards.",
                "confidence": 0.90,
            },
            headers=auth_headers,
        )
        assert t2_sub.status_code == 200, f"Failed: {t2_sub.text}"
        print("[PASS] POST /task/submit -> Task 2 submitted.")

        # Step 4: Finalize submission
        fin_resp = await ac.post(f"/submission/{sub_id}/finalize", headers=auth_headers)
        assert fin_resp.status_code == 200, f"Failed: {fin_resp.text}"
        print("[PASS] POST /submission/finalize -> 200 OK.")

    # Step 5: Scoring Pipeline Execution against PostgreSQL
    print("\n--- Scoring Engine Execution in PostgreSQL ---")
    async with session_factory() as session:
        scoring_service = ScoringService(session, SettingsService(session))
        score_res = await scoring_service.score_submission(sub_id)

        print(f"[PASS] Submission scored successfully: aggregate_score={score_res.aggregate_score:.4f}")
        print(f"[PASS] Dimension breakdown: {score_res.breakdown['dimensions']}")
        assert score_res.aggregate_score > 0.85
        assert score_res.breakdown["dimensions"]["task_success"] == 1.0

        # Step 6: Idempotent re-run check
        score_res_repeat = await scoring_service.score_submission(sub_id)
        assert score_res_repeat.aggregate_score == score_res.aggregate_score
        assert (
            score_res_repeat.breakdown["scoring_metadata"]["score_run_id"]
            == score_res.breakdown["scoring_metadata"]["score_run_id"]
        )
        print("[PASS] Idempotent scoring confirmed: identical aggregate score and run_id returned.")

        # Step 7: Team-level aggregation check
        team_summary = await scoring_service.get_team_aggregate_score(team_id)
        assert team_summary.team_score == score_res.aggregate_score
        assert team_summary.submissions_count == 1
        print(
            f"[PASS] Team aggregate score confirmed: {team_summary.team_score:.4f} (mode={team_summary.score_aggregation})"
        )

        # Step 8: Direct PostgreSQL database row verification
        sub_row = await session.get(Submission, sub_id)
        assert sub_row.aggregate_score is not None
        assert float(sub_row.aggregate_score) == score_res.aggregate_score
        assert len(sub_row.per_task_results) == 2
        for task_rec in sub_row.per_task_results:
            assert "scores" in task_rec
            assert "audit" in task_rec
            assert task_rec["scores"]["task_success"] == 1.0
        print("[PASS] Direct PostgreSQL inspection confirmed: aggregate_score, breakdown, and per_task_results stored.")

        # Teardown test tasks
        await session.execute(sa.delete(TaskAssignment).where(TaskAssignment.task_id.like("TASK-00-PG-SCORING-%")))
        await session.execute(sa.delete(Task).where(Task.task_id.like("TASK-00-PG-SCORING-%")))
        await session.commit()
        print("[PASS] Cleaned up test artifacts.")

    await engine.dispose()
    print("\n" + "=" * 64)
    print("  PHASE 4 LIVE POSTGRESQL VERIFICATION: 100% SUCCESSFUL!")
    print("=" * 64)


if __name__ == "__main__":
    asyncio.run(verify_phase4_postgres())
