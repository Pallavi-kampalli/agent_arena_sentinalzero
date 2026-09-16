import asyncio
import uuid

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
async def concurrency_fixture(db_session: AsyncSession):
    settings_service = SettingsService(db_session)
    await settings_service.seed_defaults()

    team_id = uuid.uuid4()
    token = create_bearer_token(team_id, token_version=1)
    team = Team(
        team_id=team_id,
        team_name="ConcurrencyTeam",
        members=[{"name": "Concurrent", "email": "conc@test.org"}],
        bearer_token_hash=hash_token(token),
        token_version=1,
        status="active",
    )
    db_session.add(team)

    for i in range(5):
        task_id = f"TASK-CONC-{i:03d}"
        world = generate_world(seed=5000 + i)
        task = Task(
            task_id=task_id,
            dataset="hidden",
            family="refund_request",
            variant="normal",
            input_payload={"customer_id": f"CUS-C-{i}", "customer_message": f"Conc test {i}"},
            world_state_seed=world,
            ground_truth={"expected_resolution": "refund", "must_escalate": False},
        )
        db_session.add(task)

    await settings_service.set("hidden_task_count", 5)
    await settings_service.set("submission_limit_per_team", 1)  # Limit = 1
    await db_session.commit()

    return team, token


@pytest.mark.asyncio
async def test_concurrent_submission_start_limit_race(
    client: AsyncClient, db_session: AsyncSession, concurrency_fixture
):
    """10 simultaneous POST /submission/start calls from same team must result in exactly 1 active submission."""
    team, token = concurrency_fixture
    headers = {"Authorization": f"Bearer {token}"}

    tasks = [client.post("/submission/start", headers=headers) for _ in range(10)]
    responses = await asyncio.gather(*tasks)

    status_codes = [r.status_code for r in responses]
    assert status_codes.count(200) == 1, f"Expected exactly 1 success, got {status_codes}"
    for code in status_codes:
        if code != 200:
            assert code in (403, 409)

    # Database invariant: exactly 1 submission in DB
    subs = (await db_session.execute(sa.select(Submission).where(Submission.team_id == team.team_id))).scalars().all()
    assert len(subs) == 1
    assert subs[0].attempt_number == 1
    assert subs[0].status == "in_progress"


@pytest.mark.asyncio
async def test_concurrent_task_start_no_duplicate_assignment(
    client: AsyncClient, db_session: AsyncSession, concurrency_fixture
):
    """10 simultaneous POST /task/start calls must assign exactly 1 task."""
    team, token = concurrency_fixture
    headers = {"Authorization": f"Bearer {token}"}

    # Start submission first
    sub_resp = await client.post("/submission/start", headers=headers)
    assert sub_resp.status_code == 200
    sub_id = sub_resp.json()["submission_id"]

    # Fire 10 simultaneous /task/start
    tasks = [client.post("/task/start", headers=headers) for _ in range(10)]
    responses = await asyncio.gather(*tasks)

    status_codes = [r.status_code for r in responses]
    assert status_codes.count(200) == 1, f"Expected exactly 1 task started, got {status_codes}"
    for code in status_codes:
        if code != 200:
            assert code == 409  # TASK_IN_PROGRESS

    # Database invariant: exactly 1 task assignment created
    assignments = (
        (await db_session.execute(sa.select(TaskAssignment).where(TaskAssignment.submission_id == uuid.UUID(sub_id))))
        .scalars()
        .all()
    )
    assert len(assignments) == 1


@pytest.mark.asyncio
async def test_concurrent_task_submit_race(client: AsyncClient, db_session: AsyncSession, concurrency_fixture):
    """10 simultaneous POST /task/submit calls for the same task must produce exactly 1 completion."""
    team, token = concurrency_fixture
    headers = {"Authorization": f"Bearer {token}"}

    await client.post("/submission/start", headers=headers)
    t_resp = await client.post("/task/start", headers=headers)
    task_id = t_resp.json()["task_id"]

    payload = {
        "task_id": task_id,
        "case_classification": {"category": "billing", "issue": "refund", "severity": "medium"},
        "decision": {"resolution": "refund", "escalation_required": False},
        "evidence": ["DOC-1001"],
        "customer_response": "Processed",
        "confidence": 0.9,
    }

    tasks = [client.post("/task/submit", json=payload, headers=headers) for _ in range(10)]
    responses = await asyncio.gather(*tasks)

    status_codes = [r.status_code for r in responses]
    assert status_codes.count(200) == 1, f"Expected exactly 1 submit to succeed, got {status_codes}"
    for code in status_codes:
        if code != 200:
            assert code in (404, 409)

    # Invariant: exactly 1 completion recorded
    sub = (await db_session.execute(sa.select(Submission).where(Submission.team_id == team.team_id))).scalar_one()
    results = sub.per_task_results or []
    completed = [
        r for r in results if isinstance(r, dict) and r.get("task_id") == task_id and r.get("status") == "completed"
    ]
    assert len(completed) == 1


@pytest.mark.asyncio
async def test_concurrent_finalize_race(client: AsyncClient, db_session: AsyncSession, concurrency_fixture):
    """10 simultaneous POST /submission/{id}/finalize calls must transition cleanly without duplicate side effects."""
    team, token = concurrency_fixture
    headers = {"Authorization": f"Bearer {token}"}

    sub_resp = await client.post("/submission/start", headers=headers)
    sub_id = sub_resp.json()["submission_id"]

    tasks = [client.post(f"/submission/{sub_id}/finalize", headers=headers) for _ in range(10)]
    responses = await asyncio.gather(*tasks)

    for r in responses:
        assert r.status_code == 200
        assert r.json()["status"] == "completed"

    sub = (
        await db_session.execute(sa.select(Submission).where(Submission.submission_id == uuid.UUID(sub_id)))
    ).scalar_one()
    assert sub.status == "completed"
    assert sub.completed_at is not None
