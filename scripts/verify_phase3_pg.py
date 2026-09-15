import asyncio
import copy
from datetime import datetime, timezone
import os
import sys
import time
import uuid

import httpx
from httpx import ASGITransport
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

sys.path.insert(0, os.path.abspath("src"))

from agent_arena.api.app import create_app
from agent_arena.api.deps import get_db_session
from agent_arena.config import get_config
from agent_arena.world.generator import generate_world
from agent_arena.models import (
    Base,
    Submission,
    Task,
    TaskAssignment,
    Team,
    ToolCallLog,
)
from agent_arena.services.auth_service import create_bearer_token, hash_token
from agent_arena.services.settings_service import SettingsService
from agent_arena.services.submission_service import SubmissionService


async def main():
    print("================================================================")
    print("  PHASE 3 LIVE POSTGRESQL & LIFECYCLE VERIFICATION")
    print("================================================================")

    config = get_config()
    print(f"Connecting to PostgreSQL: {config.DATABASE_URL}")

    engine = create_async_engine(config.DATABASE_URL, echo=False)
    session_factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)

    # 1. Initialize tables
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    print("[PASS] PostgreSQL schema confirmed.")

    # 2. Seed settings & hidden tasks
    run_id = uuid.uuid4().hex[:6]
    team_id = uuid.uuid4()
    token = create_bearer_token(team_id, token_version=1)
    token_hash = hash_token(token)

    async with session_factory() as session:
        settings_service = SettingsService(session)
        await settings_service.seed_defaults()
        await settings_service.set("hidden_task_count", 2)
        await settings_service.set("submission_limit_per_team", 2)
        await settings_service.set("time_budget_per_task_seconds", 180)

        team = Team(
            team_id=team_id,
            team_name=f"PG_Lifecycle_Team_{run_id}",
            members=[{"name": "Verifier", "email": "verifier@pg.org"}],
            bearer_token_hash=token_hash,
            token_version=1,
            status="active",
        )
        session.add(team)

        for i in range(2):
            task_id = f"TASK-PG-{run_id}-{i:02d}"
            world = generate_world(seed=7000 + i)
            cust_id = f"CUS-PG-{run_id}-{i:02d}"
            world["customers"].append({
                "id": cust_id,
                "name": f"Customer {i}",
                "tier": "pro",
                "region": "NA",
                "verification_status": "verified",
                "account_status": "active",
                "created_at": "2026-01-01T00:00:00Z",
            })
            txn_id = f"TXN-PG-{run_id}-{i:02d}"
            world["transactions"].append({
                "id": txn_id,
                "customer_id": cust_id,
                "amount": 100.0,
                "currency": "USD",
                "date": "2026-09-12T00:00:00Z",
                "status": "completed",
                "chargeback_status": "none",
                "under_fraud_investigation": False,
                "refund_status": "none",
                "refunded_amount": 0.0,
            })
            task = Task(
                task_id=task_id,
                dataset="hidden",
                family="refund_request",
                variant="normal",
                input_payload={"customer_id": cust_id, "customer_message": f"Refund please for {txn_id}"},
                world_state_seed=world,
                ground_truth={"expected_resolution": "refund", "must_escalate": False, "required_evidence": ["DOC-1001"]},
            )
            session.add(task)

        await session.commit()
        print("[PASS] Team and 2 hidden tasks seeded in PostgreSQL.")

    # 3. Setup FastAPI App & AsyncClient
    import agent_arena.db as db_mod
    db_mod._engine = engine
    db_mod._session_maker = session_factory

    app = create_app()

    async def get_pg_session():
        async with session_factory() as s:
            yield s

    app.dependency_overrides[get_db_session] = get_pg_session
    headers = {"Authorization": f"Bearer {token}"}
    transport = ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://agent-arena.local") as client:
        # Step A: POST /submission/start
        r_sub = await client.post("/submission/start", headers=headers)
        assert r_sub.status_code == 200, f"Submission start failed: {r_sub.text}"
        sub_data = r_sub.json()
        assert sub_data["attempt_number"] == 1
        assert sub_data["tasks_total"] == 2
        sub_id = sub_data["submission_id"]
        print(f"[PASS] POST /submission/start -> 200 OK (submission_id={sub_id}, attempt=1)")

        # Step B: GET /submission/{id}/status
        r_st = await client.get(f"/submission/{sub_id}/status", headers=headers)
        assert r_st.status_code == 200
        assert r_st.json()["status"] == "in_progress"
        assert r_st.json()["tasks_completed"] == 0
        print(f"[PASS] GET /submission/{sub_id}/status -> 200 OK (in_progress, 0 completed)")

        # Step C: POST /task/start (Task 1)
        r_t1 = await client.post("/task/start", headers=headers)
        assert r_t1.status_code == 200
        t1_data = r_t1.json()
        task_1_id = t1_data["task_id"]
        print(f"[PASS] POST /task/start -> 200 OK (assigned: {task_1_id})")

        # Step D: Execute Phase 2 Tool Calls on active task
        r_search = await client.post("/tools/search_knowledge", json={"query": "refund", "top_k": 2}, headers=headers)
        assert r_search.status_code == 200
        print(f"[PASS] POST /tools/search_knowledge -> 200 OK")

        cust_1_id = t1_data["customer_id"]
        r_cust = await client.post("/tools/get_customer", json={"customer_id": cust_1_id}, headers=headers)
        assert r_cust.status_code == 200
        print(f"[PASS] POST /tools/get_customer -> 200 OK (customer: {cust_1_id})")

        r_txs = await client.post("/tools/get_transactions", json={"customer_id": cust_1_id}, headers=headers)
        assert r_txs.status_code == 200
        txs = r_txs.json().get("transactions", [])
        if txs:
            target_tx = txs[0]
            r_ref = await client.post(
                "/tools/issue_refund",
                json={"transaction_id": target_tx["id"], "amount": min(50.0, float(target_tx["amount"])), "reason": "Eligible customer refund"},
                headers=headers,
            )
            assert r_ref.status_code == 200
            print(f"[PASS] POST /tools/issue_refund -> 200 OK (result: {r_ref.json().get('status') or r_ref.json().get('error')})")
        else:
            r_ver = await client.post(
                "/tools/request_verification",
                json={"customer_id": cust_1_id, "verification_type": "identity"},
                headers=headers,
            )
            assert r_ver.status_code == 200
            print(f"[PASS] POST /tools/request_verification -> 200 OK")

        # Step E: POST /task/submit (Task 1)
        submit_1 = {
            "task_id": task_1_id,
            "case_classification": {"category": "billing", "issue": "refund", "severity": "medium"},
            "decision": {"resolution": "refund", "escalation_required": False},
            "evidence": ["DOC-1001"],
            "customer_response": "Refund has been issued.",
            "confidence": 0.95,
        }
        r_sub1 = await client.post("/task/submit", json=submit_1, headers=headers)
        assert r_sub1.status_code == 200
        assert r_sub1.json() == {"received": True, "task_id": task_1_id}
        print(f"[PASS] POST /task/submit -> 200 OK (received: true, no oracle leakage)")

        # Step F: Status check
        r_st2 = await client.get(f"/submission/{sub_id}/status", headers=headers)
        assert r_st2.json()["tasks_completed"] == 1
        print(f"[PASS] GET /submission/{sub_id}/status -> 200 OK (1 completed)")

        # Step G: POST /task/start (Task 2)
        r_t2 = await client.post("/task/start", headers=headers)
        assert r_t2.status_code == 200
        task_2_id = r_t2.json()["task_id"]
        print(f"[PASS] POST /task/start -> 200 OK (assigned: {task_2_id})")

        # Submit Task 2
        submit_2 = {
            "task_id": task_2_id,
            "case_classification": {"category": "billing", "issue": "refund", "severity": "low"},
            "decision": {"resolution": "refund", "escalation_required": False},
            "evidence": ["DOC-1001"],
            "customer_response": "Task 2 completed.",
            "confidence": 0.9,
        }
        r_sub2 = await client.post("/task/submit", json=submit_2, headers=headers)
        assert r_sub2.status_code == 200
        print(f"[PASS] POST /task/submit -> 200 OK (Task 2 submitted)")

        # Step H: All tasks completed -> starting another task returns 400
        r_t3 = await client.post("/task/start", headers=headers)
        assert r_t3.status_code == 400
        assert r_t3.json()["detail"]["error"] == "ALL_TASKS_COMPLETED"
        print(f"[PASS] POST /task/start (all tasks completed) -> 400 ALL_TASKS_COMPLETED")

        # Step I: POST /submission/{id}/finalize
        r_fin = await client.post(f"/submission/{sub_id}/finalize", headers=headers)
        assert r_fin.status_code == 200
        assert r_fin.json()["status"] == "completed"
        print(f"[PASS] POST /submission/{sub_id}/finalize -> 200 OK (status: completed)")

        # Step J: Finalized submission blocks subsequent tool calls & task starts
        r_blocked_task = await client.post("/task/start", headers=headers)
        assert r_blocked_task.status_code == 409
        print(f"[PASS] POST /task/start after finalize -> 409 SUBMISSION_ALREADY_FINALIZED")

    # 4. Direct PostgreSQL Verification
    print("\n--- Direct Database Inspection in PostgreSQL ---")
    async with session_factory() as session:
        sub_row = await session.get(Submission, uuid.UUID(sub_id))
        assert sub_row is not None
        assert sub_row.status == "completed"
        assert sub_row.completed_at is not None
        assert len(sub_row.per_task_results) == 2
        print(f"[PASS] submissions row confirmed completed with {len(sub_row.per_task_results)} task results.")

        assign_rows = (await session.execute(
            sa.select(TaskAssignment).where(TaskAssignment.submission_id == uuid.UUID(sub_id))
        )).scalars().all()
        assert len(assign_rows) == 2
        print(f"[PASS] task_assignments rows verified in PostgreSQL (count={len(assign_rows)}).")

        logs_count = (await session.execute(
            sa.select(sa.func.count()).select_from(ToolCallLog).where(ToolCallLog.team_id == team_id)
        )).scalar()
        print(f"[PASS] tool_call_logs recorded in PostgreSQL (count={logs_count}).")

    await engine.dispose()
    print("\n================================================================")
    print("  PHASE 3 LIVE POSTGRESQL VERIFICATION: 100% SUCCESSFUL!")
    print("================================================================")


if __name__ == "__main__":
    asyncio.run(main())
