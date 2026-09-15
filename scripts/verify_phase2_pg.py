import asyncio
import os
import sys
import time
import uuid

import httpx
import sqlalchemy as sa
from httpx import ASGITransport
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

# Ensure agent_arena is importable
sys.path.insert(0, os.path.abspath("src"))

from agent_arena.api.app import create_app
from agent_arena.api.deps import get_db_session
from agent_arena.config import get_config
from agent_arena.models import (
    Base,
    Task,
    TaskAssignment,
    Team,
    ToolCallLog,
)
from agent_arena.services.auth_service import create_bearer_token, hash_token
from agent_arena.services.settings_service import SettingsService
from agent_arena.services.tool_service import ToolService
from agent_arena.world.generator import generate_world


async def main():
    print("================================================================")
    print("  PHASE 2 LIVE POSTGRESQL & LATENCY VERIFICATION")
    print("================================================================")

    config = get_config()
    print(f"Connecting to PostgreSQL: {config.DATABASE_URL}")

    engine = create_async_engine(config.DATABASE_URL, echo=False)
    session_factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)

    # 1. Initialize DB Schema in PostgreSQL
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    print("[PASS] Database tables confirmed in PostgreSQL.")

    async with session_factory() as session:
        settings_service = SettingsService(session)
        await settings_service.seed_defaults()
        print("[PASS] Settings seeded in PostgreSQL.")

        # 2. Register Team & Task
        team_id = uuid.uuid4()
        token = create_bearer_token(team_id, token_version=1)
        token_hash = hash_token(token)
        team = Team(
            team_id=team_id,
            team_name="PostgresVerificationTeam",
            members=[{"name": "Verifier", "email": "verifier@arena.org"}],
            bearer_token_hash=token_hash,
            token_version=1,
            status="active",
        )
        session.add(team)

        # Generate world state with target customer and transaction
        world = generate_world(seed=12345)
        cust_id = "CUS-VERIFY-001"
        world["customers"].append(
            {
                "id": cust_id,
                "name": "Live Verify Customer",
                "tier": "pro",
                "region": "NA",
                "verification_status": "verified",
                "account_status": "active",
                "created_at": "2026-01-01T00:00:00Z",
            }
        )
        txn_id = "TXN-VERIFY-001"
        world["transactions"].append(
            {
                "id": txn_id,
                "customer_id": cust_id,
                "amount": 75.0,
                "currency": "USD",
                "date": "2026-09-12T00:00:00Z",
                "status": "completed",
                "chargeback_status": "none",
                "under_fraud_investigation": False,
                "refund_status": "none",
                "refunded_amount": 0.0,
            }
        )
        sub_id = "SUB-VERIFY-001"
        world["subscriptions"].append(
            {
                "id": sub_id,
                "customer_id": cust_id,
                "plan_type": "pro",
                "start_date": "2026-01-01T00:00:00Z",
                "renewal_date": "2026-10-01T00:00:00Z",
                "status": "active",
                "cancellation_effective_date": None,
                "billing_cycle": "monthly",
                "auto_renew": True,
            }
        )

        task_id = f"TASK-PG-{uuid.uuid4().hex[:6]}"
        task = Task(
            task_id=task_id,
            dataset="dev",
            family="refund_request",
            variant="normal",
            input_payload={"customer_id": cust_id, "customer_message": "Need help with my transaction"},
            world_state_seed=world,
            ground_truth={"expected_resolution": "refund", "must_escalate": False, "required_evidence": ["DOC-1001"]},
        )
        session.add(task)
        await session.commit()

        # Create active assignment
        tool_service = ToolService(session, settings_service)
        assignment = await tool_service.assign_task(team_id, task_id)
        assignment_db_id = assignment.id
        print(f"[PASS] Team {team_id} and TaskAssignment {assignment_db_id} created in PostgreSQL.")

    # 3. Create FastAPI app & AsyncClient pointing to real PostgreSQL
    import agent_arena.db as db_mod

    db_mod._engine = engine
    db_mod._session_maker = session_factory

    app = create_app()

    # Override get_db_session dependency with real PG session
    async def get_pg_session():
        async with session_factory() as s:
            yield s

    app.dependency_overrides[get_db_session] = get_pg_session

    headers = {"Authorization": f"Bearer {token}"}
    transport = ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://agent-arena.local") as client:
        # Check /health
        h_resp = await client.get("/health")
        assert h_resp.status_code == 200, f"Health check failed: {h_resp.text}"
        print("[PASS] HTTP GET /health -> 200 OK")

        # Tool 1: search_knowledge
        r1 = await client.post("/tools/search_knowledge", json={"query": "refund", "top_k": 3}, headers=headers)
        assert r1.status_code == 200
        assert len(r1.json()["results"]) > 0
        print(f"[PASS] POST /tools/search_knowledge -> 200 OK (returned {len(r1.json()['results'])} results)")

        # Tool 2: get_document
        r2 = await client.post("/tools/get_document", json={"document_id": "DOC-1001"}, headers=headers)
        assert r2.status_code == 200
        assert r2.json()["document"]["id"] == "DOC-1001"
        print("[PASS] POST /tools/get_document -> 200 OK (retrieved DOC-1001)")

        # Tool 3: get_customer
        r3 = await client.post("/tools/get_customer", json={"customer_id": cust_id}, headers=headers)
        assert r3.status_code == 200
        assert r3.json()["customer"]["id"] == cust_id
        print(f"[PASS] POST /tools/get_customer -> 200 OK (customer: {cust_id})")

        # Tool 4: get_transactions
        r4 = await client.post("/tools/get_transactions", json={"customer_id": cust_id}, headers=headers)
        assert r4.status_code == 200
        assert any(t["id"] == txn_id for t in r4.json()["transactions"])
        print(f"[PASS] POST /tools/get_transactions -> 200 OK (found {len(r4.json()['transactions'])} txns)")

        # Tool 5: get_subscription
        r5 = await client.post("/tools/get_subscription", json={"customer_id": cust_id}, headers=headers)
        assert r5.status_code == 200
        assert r5.json()["subscription"]["id"] == sub_id
        print(f"[PASS] POST /tools/get_subscription -> 200 OK (subscription: {sub_id})")

        # Tool 6: get_previous_cases
        r6 = await client.post("/tools/get_previous_cases", json={"customer_id": cust_id, "limit": 3}, headers=headers)
        assert r6.status_code == 200
        assert "cases" in r6.json()
        print("[PASS] POST /tools/get_previous_cases -> 200 OK")

        # Action Tool 1: issue_refund (Eligible -> Success)
        r_ref = await client.post(
            "/tools/issue_refund",
            json={"transaction_id": txn_id, "amount": 75.0, "reason": "Customer request verified"},
            headers=headers,
        )
        assert r_ref.status_code == 200
        assert r_ref.json()["status"] == "refunded"
        assert r_ref.json()["transaction"]["refund_status"] == "refunded"
        print("[PASS] POST /tools/issue_refund (1st attempt) -> 200 OK (status: refunded)")

        # Action Tool 1b: issue_refund (Ineligible -> Rejection)
        r_ref_dupe = await client.post(
            "/tools/issue_refund",
            json={"transaction_id": txn_id, "amount": 75.0, "reason": "Attempt duplicate refund"},
            headers=headers,
        )
        assert r_ref_dupe.status_code == 200
        assert r_ref_dupe.json()["error"] == "INELIGIBLE"
        assert r_ref_dupe.json()["reason"] == "already_refunded"
        print("[PASS] POST /tools/issue_refund (duplicate) -> 200 OK (error: INELIGIBLE, reason: already_refunded)")

        # Action Tool 2: cancel_subscription
        r_cancel = await client.post(
            "/tools/cancel_subscription",
            json={"customer_id": cust_id, "subscription_id": sub_id},
            headers=headers,
        )
        assert r_cancel.status_code == 200
        print(
            f"[PASS] POST /tools/cancel_subscription -> 200 OK (result: {r_cancel.json().get('status') or r_cancel.json().get('error')})"
        )

        # Action Tool 3: escalate_case (Grounded: DOC-1001 retrieved in tool call 2)
        r_esc_good = await client.post(
            "/tools/escalate_case",
            json={"case_id": "CASE-1001", "team": "billing_tier2", "reason": "Per policy DOC-1001 exception required"},
            headers=headers,
        )
        assert r_esc_good.status_code == 200
        assert r_esc_good.json()["status"] == "escalated"
        print("[PASS] POST /tools/escalate_case (grounded with DOC-1001) -> 200 OK (status: escalated)")

        # Action Tool 3b: escalate_case (Ungrounded phantom)
        r_esc_bad = await client.post(
            "/tools/escalate_case",
            json={"case_id": "CASE-1001", "team": "billing_tier2", "reason": "Unfounded guess DOC-PHANTOM-999"},
            headers=headers,
        )
        assert r_esc_bad.status_code == 200
        assert r_esc_bad.json()["error"] == "INVALID_ESCALATION"
        assert r_esc_bad.json()["reason"] == "reason_not_grounded"
        print("[PASS] POST /tools/escalate_case (ungrounded) -> 200 OK (error: INVALID_ESCALATION)")

        # Action Tool 4: request_verification
        r_ver = await client.post(
            "/tools/request_verification",
            json={"customer_id": cust_id, "verification_type": "identity"},
            headers=headers,
        )
        assert r_ver.status_code == 200
        assert r_ver.json()["status"] == "verification_requested"
        print("[PASS] POST /tools/request_verification -> 200 OK (status: verification_requested)")

        # 4. Latency Benchmark: 50 requests across read and action tools
        print("\n--- Running Latency Benchmark (50 requests against PostgreSQL) ---")
        async with session_factory() as s:
            st = SettingsService(s)
            await st.set("tool_call_budget_per_task", 500)
            await st.set("rate_limit_tool_calls_per_min", 1000)

        latencies = []
        for i in range(50):
            t_start = time.perf_counter()
            resp = await client.post(
                "/tools/search_knowledge", json={"query": f"refund test {i}", "top_k": 3}, headers=headers
            )
            assert resp.status_code == 200, f"Call {i} failed with {resp.status_code}: {resp.text}"
            elapsed_ms = (time.perf_counter() - t_start) * 1000
            latencies.append(elapsed_ms)

        latencies.sort()
        p50 = latencies[int(len(latencies) * 0.50)]
        p95 = latencies[int(len(latencies) * 0.95)]
        p99 = latencies[int(len(latencies) * 0.99)]
        print(f"Latency p50: {p50:.2f} ms")
        print(f"Latency p95: {p95:.2f} ms  (Target: < 500 ms)")
        print(f"Latency p99: {p99:.2f} ms")
        assert p95 < 500, f"PRD §11 latency requirement failed: p95 is {p95:.2f} ms >= 500 ms"
        print(f"[PASS] PRD §11 Latency target met! (p95 = {p95:.2f} ms < 500 ms)")

    # 5. Direct Database State & Audit Inspection in PostgreSQL
    print("\n--- Direct Database Inspection in PostgreSQL ---")
    async with session_factory() as session:
        # Inspect task_assignment
        assign_row = await session.get(TaskAssignment, assignment_db_id)
        assert assign_row is not None
        runtime_tx = next(t for t in assign_row.world_runtime_state["transactions"] if t["id"] == txn_id)
        print(f"Direct DB Check: Transaction {txn_id} in world_runtime_state:")
        print(f"  - refund_status: {runtime_tx['refund_status']}")
        print(f"  - refunded_amount: {runtime_tx['refunded_amount']}")
        assert runtime_tx["refund_status"] == "refunded"
        assert runtime_tx["refunded_amount"] == 75.0
        print("[PASS] task_assignments.world_runtime_state correctly mutated and persisted in PostgreSQL.")

        # Inspect tool_call_logs
        logs_res = await session.execute(
            sa.select(ToolCallLog).where(ToolCallLog.team_id == team_id).order_by(ToolCallLog.created_at.asc())
        )
        logs = list(logs_res.scalars().all())
        print(f"Direct DB Check: Found {len(logs)} tool_call_logs recorded for team.")
        assert len(logs) >= 11

        for log in logs:
            assert log.team_id == team_id
            assert log.task_id == task_id
            assert log.latency_ms > 0
            # Ensure no bearer tokens or secret strings leaked in request payload
            req_str = str(log.request_payload)
            assert "Bearer" not in req_str
            assert token not in req_str

        # Confirm was_enforcement_rejection flags
        refund_dupe_log = next(
            log_entry
            for log_entry in logs
            if log_entry.tool_name == "issue_refund" and log_entry.response_payload.get("error") == "INELIGIBLE"
        )
        assert refund_dupe_log.was_enforcement_rejection is True
        print("[PASS] Enforcement rejection logged with was_enforcement_rejection=True.")

        escalate_ungrounded_log = next(
            log_entry
            for log_entry in logs
            if log_entry.tool_name == "escalate_case"
            and log_entry.response_payload.get("error") == "INVALID_ESCALATION"
        )
        assert escalate_ungrounded_log.was_enforcement_rejection is True
        print("[PASS] Invalid escalation logged with was_enforcement_rejection=True.")

        search_log = next(log_entry for log_entry in logs if log_entry.tool_name == "search_knowledge")
        assert search_log.was_enforcement_rejection is False
        print("[PASS] Eligible read tool logged with was_enforcement_rejection=False.")

        print("\n================================================================")
        print("  ALL PHASE 2 POSTGRESQL & RUNTIME REQUIREMENTS VERIFIED 100%!")
        print("================================================================")

    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
