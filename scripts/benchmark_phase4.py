import asyncio
import os
import sys
import time
import uuid
from datetime import datetime, timezone

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src")))

from agent_arena.config import get_config
from agent_arena.models.submission import Submission
from agent_arena.models.task import Task
from agent_arena.models.task_assignment import TaskAssignment
from agent_arena.models.team import Team
from agent_arena.models.tool_call_log import ToolCallLog
from agent_arena.scoring.service import ScoringService
from agent_arena.services.auth_service import create_bearer_token, hash_token
from agent_arena.services.settings_service import SettingsService

NUM_SUBMISSIONS = 50
TASKS_PER_SUBMISSION = 2
CONCURRENCY_WORKERS = 10


async def seed_benchmark_data(session_factory, run_id: str):
    print(
        f"Provisioning benchmark dataset ({NUM_SUBMISSIONS} submissions, {NUM_SUBMISSIONS * TASKS_PER_SUBMISSION} task assignments)..."
    )

    # 1. Base tasks for the benchmark
    async with session_factory() as session:
        settings_service = SettingsService(session)
        await settings_service.seed_defaults()
        await settings_service.set("hidden_task_count", TASKS_PER_SUBMISSION)
        await settings_service.set("competition_phase", "build")
        await session.commit()

        # Seed 2 shared template tasks in hidden pool
        task1_id = f"TASK-BENCH-{run_id}-001"
        task2_id = f"TASK-BENCH-{run_id}-002"

        t1 = Task(
            task_id=task1_id,
            dataset="hidden",
            family="refund_request",
            variant="normal",
            input_payload={"customer_id": "CUS-BENCH-1", "customer_message": "Refund please."},
            world_state_seed={
                "current_date": "2026-09-15T00:00:00Z",
                "customers": [{"id": "CUS-BENCH-1", "name": "Alice"}],
                "transactions": [
                    {
                        "id": "TXN-B-1",
                        "customer_id": "CUS-BENCH-1",
                        "amount": 100.0,
                        "refund_status": "completed",
                        "refunded_amount": 0.0,
                    }
                ],
                "policies": [{"id": "DOC-B-1", "category": "refund", "updated_at": "2026-01-01T00:00:00Z"}],
            },
            ground_truth={
                "expected_resolution": "refund",
                "must_escalate": False,
                "required_evidence": ["TXN-B-1", "DOC-B-1"],
                "expected_action": {"tool": "issue_refund", "params": {"transaction_id": "TXN-B-1", "amount": 100.0}},
                "expected_end_state": {
                    "transactions": [{"id": "TXN-B-1", "refund_status": "refunded", "refunded_amount": 100.0}]
                },
            },
        )

        t2 = Task(
            task_id=task2_id,
            dataset="hidden",
            family="subscription_cancellation",
            variant="normal",
            input_payload={"customer_id": "CUS-BENCH-2", "customer_message": "Cancel sub."},
            world_state_seed={
                "current_date": "2026-09-15T00:00:00Z",
                "customers": [{"id": "CUS-BENCH-2", "name": "Bob"}],
                "subscriptions": [{"id": "SUB-B-2", "customer_id": "CUS-BENCH-2", "status": "active"}],
                "policies": [{"id": "DOC-B-2", "category": "subscription", "updated_at": "2026-01-01T00:00:00Z"}],
            },
            ground_truth={
                "expected_resolution": "refund",
                "must_escalate": False,
                "required_evidence": ["SUB-B-2", "DOC-B-2"],
                "expected_action": {
                    "tool": "cancel_subscription",
                    "params": {"customer_id": "CUS-BENCH-2", "subscription_id": "SUB-B-2"},
                },
                "expected_end_state": {"subscriptions": [{"id": "SUB-B-2", "status": "cancelled"}]},
            },
        )
        session.add_all([t1, t2])
        await session.commit()

    # 2. Seed 50 Teams, Submissions, Assignments, and Tool Logs
    submission_ids = []
    team_ids = []

    async with session_factory() as session:
        now = datetime.now(timezone.utc)
        for i in range(NUM_SUBMISSIONS):
            team_id = uuid.uuid4()
            team_ids.append(team_id)
            sub_id = uuid.uuid4()
            submission_ids.append(sub_id)

            token = create_bearer_token(team_id, token_version=1)
            team = Team(
                team_id=team_id,
                team_name=f"BenchTeam_{run_id}_{i:03d}",
                bearer_token_hash=hash_token(token),
                token_version=1,
                status="active",
            )
            session.add(team)

            # Assign tasks based on cohort
            # Cohort A (0..29): Both tasks fully completed and correct
            # Cohort B (30..39): Wrong resolution / missing actions
            # Cohort C (40..49): 1 task completed, 1 task timed_out (early finalization test)
            if i < 30:
                per_task_results = [
                    {
                        "task_id": task1_id,
                        "status": "completed",
                        "assigned_at": now.isoformat(),
                        "submitted_at": now.isoformat(),
                        "submission_data": {
                            "decision": {"resolution": "refund", "escalation_required": False},
                            "evidence": ["TXN-B-1", "DOC-B-1"],
                            "customer_response": "Dear Alice, your refund has been processed. Best regards.",
                            "confidence": 0.95,
                        },
                    },
                    {
                        "task_id": task2_id,
                        "status": "completed",
                        "assigned_at": now.isoformat(),
                        "submitted_at": now.isoformat(),
                        "submission_data": {
                            "decision": {"resolution": "refund", "escalation_required": False},
                            "evidence": ["SUB-B-2", "DOC-B-2"],
                            "customer_response": "Dear Bob, your subscription has been cancelled. Best regards.",
                            "confidence": 0.90,
                        },
                    },
                ]
                runtime_state_1 = {
                    "transactions": [{"id": "TXN-B-1", "refund_status": "refunded", "refunded_amount": 100.0}],
                }
                runtime_state_2 = {
                    "subscriptions": [{"id": "SUB-B-2", "status": "cancelled"}],
                }
            elif i < 40:
                per_task_results = [
                    {
                        "task_id": task1_id,
                        "status": "completed",
                        "assigned_at": now.isoformat(),
                        "submitted_at": now.isoformat(),
                        "submission_data": {
                            "decision": {"resolution": "deny", "escalation_required": False},
                            "evidence": [],
                            "customer_response": "No.",
                            "confidence": 0.40,
                        },
                    },
                    {
                        "task_id": task2_id,
                        "status": "completed",
                        "assigned_at": now.isoformat(),
                        "submitted_at": now.isoformat(),
                        "submission_data": {
                            "decision": {"resolution": "escalate", "escalation_required": True},
                            "evidence": ["DOC-B-2"],
                            "customer_response": "Escalated to management.",
                            "confidence": 0.50,
                        },
                    },
                ]
                runtime_state_1 = {}
                runtime_state_2 = {}
            else:
                per_task_results = [
                    {
                        "task_id": task1_id,
                        "status": "completed",
                        "assigned_at": now.isoformat(),
                        "submitted_at": now.isoformat(),
                        "submission_data": {
                            "decision": {"resolution": "refund", "escalation_required": False},
                            "evidence": ["TXN-B-1", "DOC-B-1"],
                            "customer_response": "Processed refund successfully.",
                            "confidence": 0.85,
                        },
                    },
                    {
                        "task_id": task2_id,
                        "status": "timed_out",
                        "assigned_at": now.isoformat(),
                        "timed_out_at": now.isoformat(),
                    },
                ]
                runtime_state_1 = {
                    "transactions": [{"id": "TXN-B-1", "refund_status": "refunded", "refunded_amount": 100.0}],
                }
                runtime_state_2 = {}

            sub = Submission(
                submission_id=sub_id,
                team_id=team_id,
                attempt_number=1,
                started_at=now,
                completed_at=now,
                status="completed",
                per_task_results=per_task_results,
            )
            session.add(sub)

            # Assignments
            assign1 = TaskAssignment(
                submission_id=sub_id,
                team_id=team_id,
                task_id=task1_id,
                assigned_at=now,
                world_runtime_state=runtime_state_1,
            )
            assign2 = TaskAssignment(
                submission_id=sub_id,
                team_id=team_id,
                task_id=task2_id,
                assigned_at=now,
                world_runtime_state=runtime_state_2,
            )
            session.add_all([assign1, assign2])

            # Tool call logs
            log1 = ToolCallLog(
                team_id=team_id,
                submission_id=sub_id,
                task_id=task1_id,
                tool_name="get_document",
                request_payload={"document_id": "DOC-B-1"},
                response_payload={"id": "DOC-B-1", "category": "refund"},
                latency_ms=5,
            )
            log2 = ToolCallLog(
                team_id=team_id,
                submission_id=sub_id,
                task_id=task1_id,
                tool_name="issue_refund",
                request_payload={"transaction_id": "TXN-B-1", "amount": 100.0},
                response_payload={"status": "refunded"},
                latency_ms=10,
            )
            session.add_all([log1, log2])

        await session.commit()
        print(f"[PASS] Pre-seeded {NUM_SUBMISSIONS} submissions in PostgreSQL.")

    return submission_ids, team_ids, [task1_id, task2_id]


async def score_worker(
    worker_idx: int,
    queue: asyncio.Queue,
    session_factory,
    latencies: list[float],
):
    while True:
        sub_id = await queue.get()
        if sub_id is None:
            queue.task_done()
            break

        t0 = time.perf_counter()
        async with session_factory() as session:
            scoring_service = ScoringService(session, SettingsService(session))
            res = await scoring_service.score_submission(sub_id)
            assert res.aggregate_score is not None
        elapsed_ms = (time.perf_counter() - t0) * 1000
        latencies.append(elapsed_ms)
        queue.task_done()


async def run_benchmark():
    print("=" * 64)
    print(
        f"  PHASE 4 SCORING ENGINE BENCHMARK: {NUM_SUBMISSIONS} SUBMISSIONS ({NUM_SUBMISSIONS * TASKS_PER_SUBMISSION} TASKS)"
    )
    print("=" * 64)

    config = get_config()
    print(f"PostgreSQL target: {config.DATABASE_URL}")

    engine = create_async_engine(config.DATABASE_URL, echo=False, pool_size=20, max_overflow=20)
    session_factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)

    run_id = uuid.uuid4().hex[:6]
    sub_ids, team_ids, created_task_ids = await seed_benchmark_data(session_factory, run_id)

    # Prepare Queue
    queue = asyncio.Queue()
    for sid in sub_ids:
        queue.put_nowait(sid)

    # Launch Workers
    latencies: list[float] = []
    t_start = time.perf_counter()

    workers = [
        asyncio.create_task(score_worker(w, queue, session_factory, latencies)) for w in range(CONCURRENCY_WORKERS)
    ]

    await queue.join()

    for _ in range(CONCURRENCY_WORKERS):
        queue.put_nowait(None)
    await asyncio.gather(*workers)

    t_total = time.perf_counter() - t_start
    total_tasks = NUM_SUBMISSIONS * TASKS_PER_SUBMISSION
    sub_throughput = NUM_SUBMISSIONS / t_total
    task_throughput = total_tasks / t_total

    latencies.sort()
    p50 = latencies[int(len(latencies) * 0.50)]
    p95 = latencies[int(len(latencies) * 0.95)]
    p99 = latencies[int(len(latencies) * 0.99)]

    print("\n" + "-" * 64)
    print(f"  SCORING ENGINE PERFORMANCE METRICS ({CONCURRENCY_WORKERS} Concurrent Workers)")
    print("-" * 64)
    print(f"Total Submissions Scored : {NUM_SUBMISSIONS}")
    print(f"Total Tasks Evaluated    : {total_tasks}")
    print(f"Total Execution Time     : {t_total:.2f} s")
    print(f"Submission Throughput    : {sub_throughput:.2f} submissions/sec")
    print(f"Task Scoring Throughput  : {task_throughput:.2f} tasks/sec")
    print(f"Scoring Latency p50      : {p50:.2f} ms")
    print(f"Scoring Latency p95      : {p95:.2f} ms (Target: < 500 ms)")
    print(f"Scoring Latency p99      : {p99:.2f} ms")

    assert p95 < 500, f"Latency SLA violated: p95 = {p95:.2f} ms >= 500 ms"
    print("[PASS] Scoring Engine p95 latency is well within the SLA target (< 500 ms)!")

    # 4. Invariant Verification in PostgreSQL
    print("\n--- Verifying PostgreSQL State & Determinism ---")
    async with session_factory() as session:
        # Check all 50 submissions scored
        res = await session.execute(
            sa.select(
                Submission.submission_id, Submission.aggregate_score, Submission.breakdown, Submission.per_task_results
            ).where(Submission.submission_id.in_(sub_ids))
        )
        rows = res.fetchall()
        assert len(rows) == NUM_SUBMISSIONS
        for r in rows:
            sid, score, bd, ptr = r
            assert score is not None, f"Submission {sid} score was None"
            assert 0.0 <= float(score) <= 1.0, f"Invalid score {score}"
            assert "dimensions" in bd
            assert len(bd["dimensions"]) == 7
            assert "scoring_metadata" in bd
            assert bd["scoring_metadata"]["scoring_engine_version"] == "1.0.0"
            assert len(ptr) == TASKS_PER_SUBMISSION
            for tr in ptr:
                assert "scores" in tr
                assert "audit" in tr

        print(
            f"[PASS] 100% ({NUM_SUBMISSIONS}/{NUM_SUBMISSIONS}) submissions verified in PostgreSQL with full audit breakdown."
        )

        # Idempotency check on 5 submissions
        scoring_service = ScoringService(session, SettingsService(session))
        for sample_sub_id in sub_ids[:5]:
            re_score = await scoring_service.score_submission(sample_sub_id)
            orig_score = float((await session.get(Submission, sample_sub_id)).aggregate_score)
            assert re_score.aggregate_score == orig_score

        print("[PASS] Idempotency confirmed: repeated score_submission returns identical scores.")

        # Team aggregate score check
        sample_team_id = team_ids[0]
        team_summary = await scoring_service.get_team_aggregate_score(sample_team_id)
        assert team_summary.team_score is not None
        assert team_summary.submissions_count == 1
        print(
            f"[PASS] Team aggregation confirmed (team_score={team_summary.team_score:.4f}, count={team_summary.submissions_count})."
        )

        # Cleanup test records
        await session.execute(sa.delete(ToolCallLog).where(ToolCallLog.submission_id.in_(sub_ids)))
        await session.execute(sa.delete(TaskAssignment).where(TaskAssignment.submission_id.in_(sub_ids)))
        await session.execute(sa.delete(Submission).where(Submission.submission_id.in_(sub_ids)))
        await session.execute(sa.delete(Team).where(Team.team_id.in_(team_ids)))
        await session.execute(sa.delete(Task).where(Task.task_id.in_(created_task_ids)))
        await session.commit()
        print("[PASS] Cleaned up benchmark test data.")

    await engine.dispose()
    print("\n" + "=" * 64)
    print("  PHASE 4 BENCHMARK COMPLETED WITH 100% SUCCESS!")
    print("=" * 64)


if __name__ == "__main__":
    asyncio.run(run_benchmark())
