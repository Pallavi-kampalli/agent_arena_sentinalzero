from datetime import datetime, timedelta, timezone
import uuid
import pytest
from httpx import AsyncClient
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from agent_arena.models.task import Task
from agent_arena.models.task_assignment import TaskAssignment
from agent_arena.models.team import Team
from agent_arena.services.auth_service import create_bearer_token, hash_token
from agent_arena.services.settings_service import SettingsService
from agent_arena.world.generator import generate_world


@pytest.fixture
async def timing_fixture(db_session: AsyncSession):
    settings_service = SettingsService(db_session)
    await settings_service.seed_defaults()

    team_id = uuid.uuid4()
    token = create_bearer_token(team_id, token_version=1)
    team = Team(
        team_id=team_id,
        team_name="TimingTeam",
        members=[{"name": "Timer", "email": "timer@test.org"}],
        bearer_token_hash=hash_token(token),
        token_version=1,
        status="active",
    )
    db_session.add(team)

    for i in range(3):
        task_id = f"TASK-TIMING-{i:03d}"
        world = generate_world(seed=4000 + i)
        task = Task(
            task_id=task_id,
            dataset="hidden",
            family="refund_request",
            variant="normal",
            input_payload={"customer_id": f"CUS-T-{i}", "customer_message": f"Time test {i}"},
            world_state_seed=world,
            ground_truth={"expected_resolution": "refund", "must_escalate": False},
        )
        db_session.add(task)

    await settings_service.set("hidden_task_count", 3)
    await settings_service.set("time_budget_per_task_seconds", 60)  # 60 seconds budget
    await db_session.commit()

    return team, token


@pytest.mark.asyncio
async def test_task_time_budget_enforcement_and_auto_timeout(client: AsyncClient, db_session: AsyncSession, timing_fixture):
    team, token = timing_fixture
    headers = {"Authorization": f"Bearer {token}"}

    # Start submission & task 1
    await client.post("/submission/start", headers=headers)
    t1 = await client.post("/task/start", headers=headers)
    assert t1.status_code == 200
    task_id_1 = t1.json()["task_id"]

    # Manipulate assigned_at into the past (70s ago -> exceeds 60s budget)
    assign_row = (await db_session.execute(
        sa.select(TaskAssignment).where(TaskAssignment.task_id == task_id_1)
    )).scalar_one()
    past_time = datetime.now(timezone.utc) - timedelta(seconds=70)
    assign_row.assigned_at = past_time
    await db_session.commit()

    # 1. Tool call on expired task is rejected with 409 TASK_TIMED_OUT
    tool_resp = await client.post(
        "/tools/search_knowledge",
        json={"query": "refund", "top_k": 3},
        headers=headers,
    )
    assert tool_resp.status_code == 409
    assert tool_resp.json()["detail"]["error"] == "TASK_TIMED_OUT"

    # 2. Submit on expired task is rejected with 409 TASK_TIMED_OUT
    submit_payload = {
        "task_id": task_id_1,
        "case_classification": {"category": "billing", "issue": "refund", "severity": "medium"},
        "decision": {"resolution": "refund", "escalation_required": False},
        "evidence": ["DOC-1001"],
        "customer_response": "Late submit",
        "confidence": 0.8,
    }
    sub_resp = await client.post("/task/submit", json=submit_payload, headers=headers)
    assert sub_resp.status_code == 409
    assert sub_resp.json()["detail"]["error"] == "TASK_TIMED_OUT"

    # 3. Next /task/start auto-marks task 1 as timed_out and smoothly advances to task 2
    t2 = await client.post("/task/start", headers=headers)
    assert t2.status_code == 200
    assert t2.json()["task_id"] == "TASK-TIMING-001"

    # Submit task 2 within budget -> succeeds
    submit_payload["task_id"] = "TASK-TIMING-001"
    sub2_resp = await client.post("/task/submit", json=submit_payload, headers=headers)
    assert sub2_resp.status_code == 200


@pytest.mark.asyncio
async def test_live_settings_time_budget_extension(client: AsyncClient, db_session: AsyncSession, timing_fixture):
    team, token = timing_fixture
    headers = {"Authorization": f"Bearer {token}"}

    # Start submission & task 1
    await client.post("/submission/start", headers=headers)
    t1 = await client.post("/task/start", headers=headers)
    task_id_1 = t1.json()["task_id"]

    # Manipulate assigned_at into the past (70s ago -> exceeds original 60s budget)
    assign_row = (await db_session.execute(
        sa.select(TaskAssignment).where(TaskAssignment.task_id == task_id_1)
    )).scalar_one()
    assign_row.assigned_at = datetime.now(timezone.utc) - timedelta(seconds=70)
    await db_session.commit()

    # Organizers extend live time budget to 300s mid-event
    settings_service = SettingsService(db_session)
    await settings_service.set("time_budget_per_task_seconds", 300)
    await db_session.commit()

    # Task is now within the extended 300s budget -> submit succeeds immediately!
    submit_payload = {
        "task_id": task_id_1,
        "case_classification": {"category": "billing", "issue": "refund", "severity": "medium"},
        "decision": {"resolution": "refund", "escalation_required": False},
        "evidence": ["DOC-1001"],
        "customer_response": "Processed during extended budget",
        "confidence": 0.9,
    }
    sub_resp = await client.post("/task/submit", json=submit_payload, headers=headers)
    assert sub_resp.status_code == 200
    assert sub_resp.json() == {"received": True, "task_id": task_id_1}
