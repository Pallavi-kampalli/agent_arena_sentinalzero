import asyncio
import os
import sys
import time
import uuid

import httpx
import sqlalchemy as sa
from httpx import ASGITransport
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

sys.path.insert(0, os.path.abspath("src"))

from agent_arena.api.app import create_app
from agent_arena.api.deps import get_db_session
from agent_arena.config import get_config
from agent_arena.models import (
    Base,
    Submission,
    Task,
    TaskAssignment,
    Team,
)
from agent_arena.services.auth_service import create_bearer_token, hash_token
from agent_arena.services.settings_service import SettingsService
from agent_arena.world.generator import generate_world

NUM_TEAMS = 50


async def run_team_lifecycle(
    team_idx: int,
    client: httpx.AsyncClient,
    token: str,
    results: list[dict],
):
    headers = {"Authorization": f"Bearer {token}"}
    team_latencies = []

    # Stagger across 2 seconds
    await asyncio.sleep(team_idx * 0.04)

    # 1. Start submission
    t0 = time.perf_counter()
    r_sub = await client.post("/submission/start", headers=headers)
    team_latencies.append((time.perf_counter() - t0) * 1000)
    assert r_sub.status_code == 200, f"Team {team_idx} submission/start failed: {r_sub.text}"
    sub_id = r_sub.json()["submission_id"]

    await asyncio.sleep(0.05)

    # 2. Start task
    t0 = time.perf_counter()
    r_task = await client.post("/task/start", headers=headers)
    team_latencies.append((time.perf_counter() - t0) * 1000)
    assert r_task.status_code == 200, f"Team {team_idx} task/start failed: {r_task.text}"
    task_id = r_task.json()["task_id"]
    cust_id = r_task.json()["customer_id"]

    await asyncio.sleep(0.05)

    # 3. Read tool: search_knowledge
    t0 = time.perf_counter()
    r_search = await client.post(
        "/tools/search_knowledge", json={"query": "refund policy", "top_k": 3}, headers=headers
    )
    team_latencies.append((time.perf_counter() - t0) * 1000)
    assert r_search.status_code == 200, f"Team {team_idx} search failed: {r_search.text}"

    await asyncio.sleep(0.05)

    # 4. Read tool: get_customer
    t0 = time.perf_counter()
    r_cust = await client.post("/tools/get_customer", json={"customer_id": cust_id}, headers=headers)
    team_latencies.append((time.perf_counter() - t0) * 1000)
    assert r_cust.status_code == 200, f"Team {team_idx} get_customer failed: {r_cust.text}"

    await asyncio.sleep(0.05)

    # 5. Submit task
    submit_payload = {
        "task_id": task_id,
        "case_classification": {"category": "billing", "issue": "refund", "severity": "medium"},
        "decision": {"resolution": "refund", "escalation_required": False},
        "evidence": ["DOC-1001"],
        "customer_response": f"Team {team_idx} resolved your refund request.",
        "confidence": 0.9,
    }
    t0 = time.perf_counter()
    r_submit = await client.post("/task/submit", json=submit_payload, headers=headers)
    team_latencies.append((time.perf_counter() - t0) * 1000)
    assert r_submit.status_code == 200, f"Team {team_idx} submit failed: {r_submit.text}"
    assert r_submit.json() == {"received": True, "task_id": task_id}

    await asyncio.sleep(0.05)

    # 6. Status check
    t0 = time.perf_counter()
    r_status = await client.get(f"/submission/{sub_id}/status", headers=headers)
    team_latencies.append((time.perf_counter() - t0) * 1000)
    assert r_status.status_code == 200, f"Team {team_idx} status failed: {r_status.text}"
    assert r_status.json()["tasks_completed"] == 1

    await asyncio.sleep(0.05)

    # 7. Finalize submission
    t0 = time.perf_counter()
    r_fin = await client.post(f"/submission/{sub_id}/finalize", headers=headers)
    team_latencies.append((time.perf_counter() - t0) * 1000)
    assert r_fin.status_code == 200, f"Team {team_idx} finalize failed: {r_fin.text}"

    results.append(
        {
            "team_idx": team_idx,
            "sub_id": sub_id,
            "task_id": task_id,
            "latencies": team_latencies,
        }
    )


async def main():
    print("================================================================")
    print(f"  PHASE 3 CONCURRENT LOAD BENCHMARK: {NUM_TEAMS} TEAMS")
    print("================================================================")

    config = get_config()
    print(f"PostgreSQL target: {config.DATABASE_URL}")

    engine = create_async_engine(config.DATABASE_URL, echo=False, pool_size=50, max_overflow=50)
    session_factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    teams_data = []
    run_id = uuid.uuid4().hex[:6]

    async with session_factory() as session:
        settings_service = SettingsService(session)
        await settings_service.seed_defaults()
        await settings_service.set("hidden_task_count", 1)  # 1 task per benchmark run
        await settings_service.set("submission_limit_per_team", 5)
        await settings_service.set("rate_limit_tool_calls_per_min", 2000)
        await settings_service.set("tool_call_budget_per_task", 500)
        await settings_service.set("time_budget_per_task_seconds", 300)

        print(f"Provisioning {NUM_TEAMS} independent teams & tasks...")
        for i in range(NUM_TEAMS):
            team_id = uuid.uuid4()
            token = create_bearer_token(team_id, token_version=1)
            team = Team(
                team_id=team_id,
                team_name=f"LoadTeam_P3_{run_id}_{i:03d}",
                members=[{"name": f"Member_{i}", "email": f"team{i}@load.org"}],
                bearer_token_hash=hash_token(token),
                token_version=1,
                status="active",
            )
            session.add(team)
            teams_data.append({"team_idx": i, "team": team, "token": token})

        # Ensure hidden pool has enough tasks
        pool_count = (
            await session.execute(sa.select(sa.func.count()).select_from(Task).where(Task.dataset == "hidden"))
        ).scalar() or 0
        if pool_count < 1:
            for i in range(5):
                world = generate_world(seed=8000 + i)
                task = Task(
                    task_id=f"TASK-LOAD-P3-{run_id}-{i}",
                    dataset="hidden",
                    family="refund_request",
                    variant="normal",
                    input_payload={"customer_id": f"CUS-LOAD-{i}", "customer_message": f"Help {i}"},
                    world_state_seed=world,
                    ground_truth={"expected_resolution": "refund", "must_escalate": False},
                )
                session.add(task)

        await session.commit()
        print(f"[PASS] Provisioned {NUM_TEAMS} teams in PostgreSQL.")

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
    print(f"\nLaunching {NUM_TEAMS} concurrent team lifecycles (7 requests each = {NUM_TEAMS * 7} total requests)...")
    benchmark_results = []
    t_benchmark_start = time.perf_counter()

    async with httpx.AsyncClient(transport=transport, base_url="http://agent-arena.local", timeout=30.0) as client:
        team_tasks = [
            run_team_lifecycle(
                team_idx=td["team_idx"],
                client=client,
                token=td["token"],
                results=benchmark_results,
            )
            for td in teams_data
        ]
        await asyncio.gather(*team_tasks)

    total_time = time.perf_counter() - t_benchmark_start
    total_reqs = NUM_TEAMS * 7
    throughput = total_reqs / total_time

    # 4. Latency Analysis
    all_latencies = []
    for res in benchmark_results:
        all_latencies.extend(res["latencies"])

    all_latencies.sort()
    p50 = all_latencies[int(len(all_latencies) * 0.50)]
    p95 = all_latencies[int(len(all_latencies) * 0.95)]
    p99 = all_latencies[int(len(all_latencies) * 0.99)]

    print(f"\n--- Phase 3 Benchmark Results ({total_reqs} requests total across {NUM_TEAMS} teams) ---")
    print(f"Total Duration : {total_time:.2f} s")
    print(f"Throughput     : {throughput:.2f} requests/sec")
    print(f"Client RTT p50 : {p50:.2f} ms")
    print(f"Client RTT p95 : {p95:.2f} ms (Target: < 500 ms)")
    print(f"Client RTT p99 : {p99:.2f} ms")
    assert p95 < 500, f"Latency SLA violated: p95 = {p95:.2f} ms >= 500 ms"
    print("[PASS] PRD §11 Latency target (< 500 ms p95) satisfied under 50-team concurrent load!")

    # 5. Verify PostgreSQL Data Invariants
    print("\n--- Verifying PostgreSQL Data Invariants Across All 50 Teams ---")
    async with session_factory() as session:
        for res in benchmark_results:
            sub = await session.get(Submission, uuid.UUID(res["sub_id"]))
            assert sub is not None
            assert sub.status == "completed"
            assert sub.attempt_number == 1
            assert len(sub.per_task_results) == 1
            assert sub.per_task_results[0]["status"] == "completed"

            # Check assignment row exists
            assign = (
                await session.execute(
                    sa.select(TaskAssignment).where(TaskAssignment.submission_id == uuid.UUID(res["sub_id"]))
                )
            ).scalar_one_or_none()
            assert assign is not None

    print("[PASS] 100% of submissions transitioned to completed with verified task results.")
    print("================================================================")
    print("  PHASE 3 50-TEAM CONCURRENT BENCHMARK COMPLETED SUCCESSFULLY!")
    print("================================================================")

    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
