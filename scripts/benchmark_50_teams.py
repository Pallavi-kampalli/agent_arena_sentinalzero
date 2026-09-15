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

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

sys.path.insert(0, os.path.abspath("src"))

from agent_arena.api.app import create_app
from agent_arena.api.deps import get_db_session
from agent_arena.config import get_config
from agent_arena.world.generator import generate_world
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


NUM_TEAMS = 50


async def run_team_workflow(
    team_idx: int,
    client: httpx.AsyncClient,
    team: Team,
    token: str,
    task_id: str,
    cust_id: str,
    txn_id: str,
    results: list[dict],
):
    headers = {"Authorization": f"Bearer {token}"}
    team_latencies = []

    # Natural stagger across initial 2-second competition window
    await asyncio.sleep(team_idx * 0.04)

    # 1. Search knowledge
    t0 = time.perf_counter()
    r1 = await client.post("/tools/search_knowledge", json={"query": "refund policy", "top_k": 3}, headers=headers)
    team_latencies.append((time.perf_counter() - t0) * 1000)
    assert r1.status_code == 200, f"Team {team_idx} search failed: {r1.status_code}"

    # Agent think time (simulating LLM decision turn ~100ms)
    await asyncio.sleep(0.1)

    # 2. Get customer
    t0 = time.perf_counter()
    r2 = await client.post("/tools/get_customer", json={"customer_id": cust_id}, headers=headers)
    team_latencies.append((time.perf_counter() - t0) * 1000)
    assert r2.status_code == 200, f"Team {team_idx} get_customer failed: {r2.status_code}"
    assert r2.json()["customer"]["id"] == cust_id

    # Agent think time
    await asyncio.sleep(0.1)

    # 3. Issue refund (Team specific amount = 10.0 + team_idx)
    refund_amt = 10.0 + team_idx
    t0 = time.perf_counter()
    r3 = await client.post(
        "/tools/issue_refund",
        json={"transaction_id": txn_id, "amount": refund_amt, "reason": f"Team {team_idx} legitimate refund"},
        headers=headers,
    )
    team_latencies.append((time.perf_counter() - t0) * 1000)
    assert r3.status_code == 200, f"Team {team_idx} refund failed: {r3.status_code}"
    assert r3.json()["status"] == "partially_refunded"
    assert r3.json()["transaction"]["refunded_amount"] == refund_amt

    # Agent think time
    await asyncio.sleep(0.1)

    # 4. Get transactions to verify own state
    t0 = time.perf_counter()
    r4 = await client.post("/tools/get_transactions", json={"customer_id": cust_id}, headers=headers)
    team_latencies.append((time.perf_counter() - t0) * 1000)
    assert r4.status_code == 200, f"Team {team_idx} get_transactions failed: {r4.status_code}"
    tx = next(t for t in r4.json()["transactions"] if t["id"] == txn_id)
    assert tx["refunded_amount"] == refund_amt

    results.append({
        "team_idx": team_idx,
        "team_id": team.team_id,
        "latencies": team_latencies,
        "refund_amt": refund_amt,
    })


async def main():
    print("================================================================")
    print(f"  PHASE 2 CONCURRENT LOAD BENCHMARK: {NUM_TEAMS} TEAMS")
    print("================================================================")

    config = get_config()
    print(f"PostgreSQL target: {config.DATABASE_URL}")

    engine = create_async_engine(config.DATABASE_URL, echo=False, pool_size=50, max_overflow=50)
    session_factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)

    # 1. Ensure DB Schema
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    teams_data = []

    async with session_factory() as session:
        settings_service = SettingsService(session)
        await settings_service.seed_defaults()
        # Ensure limits accommodate load benchmark
        await settings_service.set("rate_limit_tool_calls_per_min", 2000)
        await settings_service.set("tool_call_budget_per_task", 500)

        print(f"Registering {NUM_TEAMS} independent teams & provisioning tasks...")
        tool_service = ToolService(session, settings_service)

        run_id = uuid.uuid4().hex[:6]
        for i in range(NUM_TEAMS):
            team_id = uuid.uuid4()
            token = create_bearer_token(team_id, token_version=1)
            token_hash = hash_token(token)
            team = Team(
                team_id=team_id,
                team_name=f"LoadTeam_{run_id}_{i:03d}",
                members=[{"name": f"Member_{i}", "email": f"team{i}@loadtest.org"}],
                bearer_token_hash=token_hash,
                token_version=1,
                status="active",
            )
            session.add(team)

            world = generate_world(seed=1000 + i)
            cust_id = f"CUS-LOAD-{run_id}-{i:03d}"
            world["customers"].append({
                "id": cust_id,
                "name": f"Customer {i}",
                "tier": "pro",
                "region": "NA",
                "verification_status": "verified",
                "account_status": "active",
                "created_at": "2026-01-01T00:00:00Z",
            })
            txn_id = f"TXN-LOAD-{run_id}-{i:03d}"
            world["transactions"].append({
                "id": txn_id,
                "customer_id": cust_id,
                "amount": 200.0,
                "currency": "USD",
                "date": "2026-09-12T00:00:00Z",
                "status": "completed",
                "chargeback_status": "none",
                "under_fraud_investigation": False,
                "refund_status": "none",
                "refunded_amount": 0.0,
            })

            task_id = f"TASK-LOAD-{run_id}-{i:03d}"
            task = Task(
                task_id=task_id,
                dataset="dev",
                family="refund_request",
                variant="normal",
                input_payload={"customer_id": cust_id, "customer_message": "Load test"},
                world_state_seed=copy.deepcopy(world),
                ground_truth={"expected_resolution": "refund", "must_escalate": False, "required_evidence": ["DOC-1001"]},
            )
            session.add(task)
            await session.commit()

            assignment = await tool_service.assign_task(team_id, task_id)
            teams_data.append({
                "team_idx": i,
                "team": team,
                "token": token,
                "task_id": task_id,
                "assignment_id": assignment.id,
                "cust_id": cust_id,
                "txn_id": txn_id,
                "seed": copy.deepcopy(task.world_state_seed),
            })

        print(f"[PASS] Successfully provisioned {NUM_TEAMS} teams and assignments.")

    # 2. Setup FastAPI App & AsyncClient
    import agent_arena.db as db_mod
    db_mod._engine = engine
    db_mod._session_maker = session_factory

    app = create_app()

    async def get_pg_session():
        async with session_factory() as s:
            yield s

    app.dependency_overrides[get_db_session] = get_pg_session
    transport = ASGITransport(app=app)

    # 3. Fire all 50 teams concurrently
    print(f"\nLaunching {NUM_TEAMS} concurrent team workloads (4 requests each = {NUM_TEAMS * 4} total requests)...")
    benchmark_results = []
    t_benchmark_start = time.perf_counter()

    async with httpx.AsyncClient(transport=transport, base_url="http://agent-arena.local", timeout=30.0) as client:
        team_tasks = [
            run_team_workflow(
                team_idx=td["team_idx"],
                client=client,
                team=td["team"],
                token=td["token"],
                task_id=td["task_id"],
                cust_id=td["cust_id"],
                txn_id=td["txn_id"],
                results=benchmark_results,
            )
            for td in teams_data
        ]
        await asyncio.gather(*team_tasks)

    total_time = time.perf_counter() - t_benchmark_start
    total_reqs = NUM_TEAMS * 4
    throughput = total_reqs / total_time

    # 4. Latency Analysis
    all_latencies = []
    for res in benchmark_results:
        all_latencies.extend(res["latencies"])

    all_latencies.sort()
    p50 = all_latencies[int(len(all_latencies) * 0.50)]
    p95 = all_latencies[int(len(all_latencies) * 0.95)]
    p99 = all_latencies[int(len(all_latencies) * 0.99)]

    print(f"\n--- Benchmark Results ({total_reqs} requests total across {NUM_TEAMS} teams) ---")
    print(f"Total Duration : {total_time:.2f} s")
    print(f"Throughput     : {throughput:.2f} requests/sec")
    print(f"Client RTT p50 : {p50:.2f} ms")
    print(f"Client RTT p95 : {p95:.2f} ms (Target: < 500 ms)")
    print(f"Client RTT p99 : {p99:.2f} ms")
    assert p95 < 500, f"Latency SLA violated: p95 = {p95:.2f} ms >= 500 ms"
    print("[PASS] PRD §11 Latency target (< 500 ms p95) satisfied under 50-team concurrent load!")

    # 5. Verify Isolation & Data Integrity Across All 50 Teams
    print("\n--- Verifying Strict Cross-Team Isolation in PostgreSQL ---")
    async with session_factory() as session:
        for td in teams_data:
            i = td["team_idx"]
            assign = await session.get(TaskAssignment, td["assignment_id"])
            expected_refund = 10.0 + i
            tx = next(t for t in assign.world_runtime_state["transactions"] if t["id"] == td["txn_id"])
            assert tx["refunded_amount"] == expected_refund, (
                f"Cross-team corruption! Team {i} expected {expected_refund} but got {tx['refunded_amount']}"
            )
            assert tx["refund_status"] == "partially_refunded"

            # Check task seed immutability
            task = await session.get(Task, td["task_id"])
            assert task.world_state_seed == td["seed"], f"Task {td['task_id']} seed mutated!"

            # Check tool call logs count & token scrubbing
            logs = (
                await session.execute(
                    sa.select(ToolCallLog).where(ToolCallLog.team_id == td["team"].team_id)
                )
            ).scalars().all()
            assert len(logs) == 4, f"Team {i} logged {len(logs)} calls, expected 4"
            for log in logs:
                req_str = str(log.request_payload)
                assert td["token"] not in req_str, f"Token leaked in log for team {i}"
                assert "Bearer" not in req_str

    print(f"[PASS] Verified 100% data isolation and zero cross-team leakage across all {NUM_TEAMS} teams.")
    print("================================================================")
    print("  50-TEAM CONCURRENT LOAD BENCHMARK COMPLETED SUCCESSFULLY!")
    print("================================================================")

    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
