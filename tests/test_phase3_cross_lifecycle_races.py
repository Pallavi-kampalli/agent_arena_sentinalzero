import asyncio
import uuid
from datetime import UTC, datetime, timedelta

import pytest
import sqlalchemy as sa
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from agent_arena.models.submission import Submission
from agent_arena.models.task import Task
from agent_arena.models.task_assignment import TaskAssignment
from agent_arena.models.team import Team
from agent_arena.services.auth_service import create_bearer_token, hash_token
from agent_arena.services.settings_service import SettingsService
from conftest import generate_world


@pytest.fixture
async def setup_cross_lifecycle(db_session: AsyncSession):
    settings_service = SettingsService(db_session)
    await settings_service.seed_defaults()

    # Team 1
    t1_id = uuid.uuid4()
    token_1 = create_bearer_token(t1_id, token_version=1)
    team_1 = Team(
        team_id=t1_id,
        team_name="TeamOne_CrossRace",
        members=[{"name": "Alice"}],
        bearer_token_hash=hash_token(token_1),
        status="active",
    )
    db_session.add(team_1)

    # Team 2
    t2_id = uuid.uuid4()
    token_2 = create_bearer_token(t2_id, token_version=1)
    team_2 = Team(
        team_id=t2_id,
        team_name="TeamTwo_CrossRace",
        members=[{"name": "Bob"}],
        bearer_token_hash=hash_token(token_2),
        status="active",
    )
    db_session.add(team_2)

    # Seed 3 hidden tasks
    for i in range(3):
        task_id = f"TASK-REUSE-{i:03d}"
        world = generate_world(seed=9000 + i)
        world["customers"].append(
            {
                "id": f"CUS-REUSE-{i}",
                "name": f"Customer {i}",
                "tier": "pro",
                "region": "NA",
                "verification_status": "verified",
                "account_status": "active",
                "created_at": "2026-01-01T00:00:00Z",
            }
        )
        world["transactions"].append(
            {
                "id": f"TXN-REUSE-{i}",
                "customer_id": f"CUS-REUSE-{i}",
                "amount": 200.0,
                "currency": "USD",
                "date": "2026-09-12T00:00:00Z",
                "status": "completed",
                "chargeback_status": "none",
                "under_fraud_investigation": False,
                "refund_status": "none",
                "refunded_amount": 0.0,
            }
        )
        task = Task(
            task_id=task_id,
            dataset="hidden",
            family="refund_request",
            variant="normal",
            input_payload={"customer_id": f"CUS-REUSE-{i}", "customer_message": f"Help {i}"},
            world_state_seed=world,
            ground_truth={"expected_resolution": "refund", "must_escalate": False, "required_evidence": ["DOC-1001"]},
        )
        db_session.add(task)

    await settings_service.set("hidden_task_count", 3)
    await settings_service.set("submission_limit_per_team", 5)
    await settings_service.set("time_budget_per_task_seconds", 60)
    await db_session.commit()

    return (team_1, token_1), (team_2, token_2)


@pytest.mark.asyncio
async def test_model_b_task_reuse_across_teams(client: AsyncClient, db_session: AsyncSession, setup_cross_lifecycle):
    """Model B Proof: Multiple teams receive the same task definition but have completely disjoint mutable world states."""
    (team_1, token_1), (team_2, token_2) = setup_cross_lifecycle
    headers_1 = {"Authorization": f"Bearer {token_1}"}
    headers_2 = {"Authorization": f"Bearer {token_2}"}

    # Both teams start a submission
    r_sub1 = await client.post("/submission/start", headers=headers_1)
    r_sub2 = await client.post("/submission/start", headers=headers_2)
    assert r_sub1.status_code == 200
    assert r_sub2.status_code == 200
    sub_id_1 = r_sub1.json()["submission_id"]
    sub_id_2 = r_sub2.json()["submission_id"]

    # Both teams start their first task -> both get TASK-REUSE-000
    r_t1 = await client.post("/task/start", headers=headers_1)
    r_t2 = await client.post("/task/start", headers=headers_2)
    assert r_t1.status_code == 200
    assert r_t2.status_code == 200
    assert r_t1.json()["task_id"] == "TASK-REUSE-000"
    assert r_t2.json()["task_id"] == "TASK-REUSE-000"

    # Team 1 refunds $30.0
    r_ref1 = await client.post(
        "/tools/issue_refund",
        json={"transaction_id": "TXN-REUSE-0", "amount": 30.0, "reason": "Team 1 partial refund"},
        headers=headers_1,
    )
    assert r_ref1.status_code == 200
    assert r_ref1.json()["transaction"]["refunded_amount"] == 30.0

    # Team 2 refunds $75.0
    r_ref2 = await client.post(
        "/tools/issue_refund",
        json={"transaction_id": "TXN-REUSE-0", "amount": 75.0, "reason": "Team 2 partial refund"},
        headers=headers_2,
    )
    assert r_ref2.status_code == 200
    assert r_ref2.json()["transaction"]["refunded_amount"] == 75.0

    # Verify direct database isolation
    a1 = (
        await db_session.execute(sa.select(TaskAssignment).where(TaskAssignment.submission_id == uuid.UUID(sub_id_1)))
    ).scalar_one()
    a2 = (
        await db_session.execute(sa.select(TaskAssignment).where(TaskAssignment.submission_id == uuid.UUID(sub_id_2)))
    ).scalar_one()

    tx1 = next(t for t in a1.world_runtime_state["transactions"] if t["id"] == "TXN-REUSE-0")
    tx2 = next(t for t in a2.world_runtime_state["transactions"] if t["id"] == "TXN-REUSE-0")
    assert tx1["refunded_amount"] == 30.0
    assert tx2["refunded_amount"] == 75.0

    # Verify seed immutability in tasks table
    task_row = (await db_session.execute(sa.select(Task).where(Task.task_id == "TASK-REUSE-000"))).scalar_one()
    seed_tx = next(t for t in task_row.world_state_seed["transactions"] if t["id"] == "TXN-REUSE-0")
    assert seed_tx["refunded_amount"] == 0.0
    assert seed_tx["refund_status"] == "none"


@pytest.mark.asyncio
async def test_task_timeout_does_not_expire_submission(
    client: AsyncClient, db_session: AsyncSession, setup_cross_lifecycle
):
    """Proves Task Timeout != Submission Expiration."""
    (team_1, token_1), _ = setup_cross_lifecycle
    headers = {"Authorization": f"Bearer {token_1}"}

    # Start submission and task 1
    r_sub = await client.post("/submission/start", headers=headers)
    sub_id = r_sub.json()["submission_id"]
    r_t1 = await client.post("/task/start", headers=headers)
    task_id_1 = r_t1.json()["task_id"]

    # Manipulate task 1 assigned_at to 80 seconds in the past (> 60s budget)
    assign_row = (
        await db_session.execute(sa.select(TaskAssignment).where(TaskAssignment.task_id == task_id_1))
    ).scalar_one()
    assign_row.assigned_at = datetime.now(UTC) - timedelta(seconds=80)
    await db_session.commit()

    # Task is timed out, but SUBMISSION REMAINS IN PROGRESS
    st_resp = await client.get(f"/submission/{sub_id}/status", headers=headers)
    assert st_resp.status_code == 200
    assert st_resp.json()["status"] == "in_progress"
    assert st_resp.json()["tasks_completed"] == 0

    # Team starts next task -> advances cleanly to Task 2!
    r_t2 = await client.post("/task/start", headers=headers)
    assert r_t2.status_code == 200
    task_id_2 = r_t2.json()["task_id"]
    assert task_id_2 == "TASK-REUSE-001"

    # Submit task 2 within budget -> succeeds!
    sub_payload = {
        "task_id": task_id_2,
        "case_classification": {"category": "billing", "issue": "refund", "severity": "medium"},
        "decision": {"resolution": "refund", "escalation_required": False},
        "evidence": ["DOC-1001"],
        "customer_response": "Task 2 completed.",
        "confidence": 0.9,
    }
    r_sub2 = await client.post("/task/submit", json=sub_payload, headers=headers)
    assert r_sub2.status_code == 200

    # Finalize submission -> transitions to completed
    fin_resp = await client.post(f"/submission/{sub_id}/finalize", headers=headers)
    assert fin_resp.status_code == 200
    assert fin_resp.json()["status"] == "completed"

    # Database proof: Task 1 is timed_out, Task 2 is completed
    sub = await db_session.get(Submission, uuid.UUID(sub_id))
    results = sub.per_task_results
    assert len(results) == 2
    assert results[0]["task_id"] == task_id_1 and results[0]["status"] == "timed_out"
    assert results[1]["task_id"] == task_id_2 and results[1]["status"] == "completed"


@pytest.mark.asyncio
async def test_competition_window_expires_submission(
    client: AsyncClient, db_session: AsyncSession, setup_cross_lifecycle
):
    """Proves that competition freeze or end time expires the entire submission and blocks operations."""
    (team_1, token_1), _ = setup_cross_lifecycle
    headers = {"Authorization": f"Bearer {token_1}"}

    r_sub = await client.post("/submission/start", headers=headers)
    sub_id = r_sub.json()["submission_id"]
    await client.post("/task/start", headers=headers)

    # Admin freezes competition
    settings_service = SettingsService(db_session)
    await settings_service.set("competition_phase", "frozen")
    await db_session.commit()

    # Starting a new task is rejected with 409 COMPETITION_FROZEN
    r_task = await client.post("/task/start", headers=headers)
    assert r_task.status_code == 409
    assert r_task.json()["detail"]["error"] == "COMPETITION_FROZEN"

    # Submission status transitions to expired
    sub = await db_session.get(Submission, uuid.UUID(sub_id))
    assert sub.status == "expired"


@pytest.mark.asyncio
async def test_no_resurrection_after_recorded_timeout(
    client: AsyncClient, db_session: AsyncSession, setup_cross_lifecycle
):
    """Proves that a task recorded as timed_out can NEVER be resubmitted, even if the budget is later increased to 10,000s."""
    (team_1, token_1), _ = setup_cross_lifecycle
    headers = {"Authorization": f"Bearer {token_1}"}

    await client.post("/submission/start", headers=headers)
    t1 = await client.post("/task/start", headers=headers)
    task_id = t1.json()["task_id"]

    # Expire task and trigger auto-marking
    assign_row = (
        await db_session.execute(sa.select(TaskAssignment).where(TaskAssignment.task_id == task_id))
    ).scalar_one()
    assign_row.assigned_at = datetime.now(UTC) - timedelta(seconds=80)
    await db_session.commit()

    payload = {
        "task_id": task_id,
        "case_classification": {"category": "billing", "issue": "refund", "severity": "medium"},
        "decision": {"resolution": "refund", "escalation_required": False},
        "evidence": ["DOC-1001"],
        "customer_response": "Late submit",
        "confidence": 0.8,
    }
    # First submit records timeout
    r1 = await client.post("/task/submit", json=payload, headers=headers)
    assert r1.status_code == 409
    assert r1.json()["detail"]["error"] == "TASK_TIMED_OUT"

    # Admin increases budget to 10,000s
    settings_service = SettingsService(db_session)
    await settings_service.set("time_budget_per_task_seconds", 10000)
    await db_session.commit()

    # Second submit MUST STILL BE REJECTED!
    r2 = await client.post("/task/submit", json=payload, headers=headers)
    assert r2.status_code == 409
    assert r2.json()["detail"]["error"] == "TASK_TIMED_OUT"


@pytest.mark.asyncio
async def test_finalization_with_incomplete_and_timed_out_tasks(
    client: AsyncClient, db_session: AsyncSession, setup_cross_lifecycle
):
    """Proves that finalization distinguishes completed tasks from incomplete/timed-out tasks and never awards completion credit to uncompleted work."""
    (team_1, token_1), _ = setup_cross_lifecycle
    headers = {"Authorization": f"Bearer {token_1}"}

    # Start submission with hidden_task_count = 3
    r_sub = await client.post("/submission/start", headers=headers)
    sub_id = r_sub.json()["submission_id"]

    # Complete Task 1
    t1 = await client.post("/task/start", headers=headers)
    t1_id = t1.json()["task_id"]
    payload = {
        "task_id": t1_id,
        "case_classification": {"category": "billing", "issue": "refund", "severity": "medium"},
        "decision": {"resolution": "refund", "escalation_required": False},
        "evidence": ["DOC-1001"],
        "customer_response": "Done",
        "confidence": 0.9,
    }
    await client.post("/task/submit", json=payload, headers=headers)

    # Start Task 2 (leaves it in-progress without submitting)
    await client.post("/task/start", headers=headers)

    # Never start Task 3. Call finalize immediately.
    fin = await client.post(f"/submission/{sub_id}/finalize", headers=headers)
    assert fin.status_code == 200
    assert fin.json()["status"] == "completed"

    # Status check confirms ONLY 1 task completed out of 3
    st = await client.get(f"/submission/{sub_id}/status", headers=headers)
    assert st.json()["status"] == "completed"
    assert st.json()["tasks_completed"] == 1
    assert st.json()["tasks_total"] == 3

    # Database inspection
    sub = await db_session.get(Submission, uuid.UUID(sub_id))
    results = sub.per_task_results
    assert len(results) == 2
    assert results[0]["task_id"] == t1_id and results[0]["status"] == "completed"
    assert results[1]["status"] == "timed_out"  # Unsubmitted Task 2 marked timed_out


@pytest.mark.asyncio
async def test_race_task_start_vs_task_submit(client: AsyncClient, db_session: AsyncSession, setup_cross_lifecycle):
    """Simultaneous POST /task/submit and POST /task/start are serialized cleanly by row locks without deadlock."""
    (team_1, token_1), _ = setup_cross_lifecycle
    headers = {"Authorization": f"Bearer {token_1}"}

    await client.post("/submission/start", headers=headers)
    t1 = await client.post("/task/start", headers=headers)
    t1_id = t1.json()["task_id"]

    payload = {
        "task_id": t1_id,
        "case_classification": {"category": "billing", "issue": "refund", "severity": "medium"},
        "decision": {"resolution": "refund", "escalation_required": False},
        "evidence": ["DOC-1001"],
        "customer_response": "Done",
        "confidence": 0.9,
    }

    # Fire submit and start simultaneously
    req_submit = client.post("/task/submit", json=payload, headers=headers)
    req_start = client.post("/task/start", headers=headers)
    res_submit, res_start = await asyncio.gather(req_submit, req_start)

    # Both requests must complete with valid status codes (200 or 409) without 500 or deadlock
    assert res_submit.status_code in (200, 409)
    assert res_start.status_code in (200, 409)


@pytest.mark.asyncio
async def test_race_task_submit_vs_finalize(client: AsyncClient, db_session: AsyncSession, setup_cross_lifecycle):
    """Simultaneous POST /task/submit and POST /submission/{id}/finalize are serialized cleanly without deadlock."""
    (team_1, token_1), _ = setup_cross_lifecycle
    headers = {"Authorization": f"Bearer {token_1}"}

    r_sub = await client.post("/submission/start", headers=headers)
    sub_id = r_sub.json()["submission_id"]
    t1 = await client.post("/task/start", headers=headers)
    t1_id = t1.json()["task_id"]

    payload = {
        "task_id": t1_id,
        "case_classification": {"category": "billing", "issue": "refund", "severity": "medium"},
        "decision": {"resolution": "refund", "escalation_required": False},
        "evidence": ["DOC-1001"],
        "customer_response": "Done",
        "confidence": 0.9,
    }

    req_submit = client.post("/task/submit", json=payload, headers=headers)
    req_fin = client.post(f"/submission/{sub_id}/finalize", headers=headers)
    res_submit, res_fin = await asyncio.gather(req_submit, req_fin)

    assert res_fin.status_code == 200
    assert res_submit.status_code in (200, 409)

    sub = await db_session.get(Submission, uuid.UUID(sub_id))
    assert sub.status == "completed"
