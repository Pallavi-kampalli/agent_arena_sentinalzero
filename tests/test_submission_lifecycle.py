import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from agent_arena.models.task import Task
from agent_arena.models.team import Team
from agent_arena.services.auth_service import create_bearer_token, hash_token
from agent_arena.services.settings_service import SettingsService
from conftest import generate_world


@pytest.fixture
async def registered_team(db_session: AsyncSession):
    settings_service = SettingsService(db_session)
    await settings_service.seed_defaults()

    team_id = uuid.uuid4()
    token = create_bearer_token(team_id, token_version=1)
    token_hash = hash_token(token)

    team = Team(
        team_id=team_id,
        team_name="LifecycleTestTeam",
        members=[{"name": "Tester", "email": "test@test.org"}],
        bearer_token_hash=token_hash,
        token_version=1,
        status="active",
    )
    db_session.add(team)

    # Seed 5 hidden tasks
    for i in range(5):
        task_id = f"TASK-HIDDEN-{i:03d}"
        world = generate_world(seed=2000 + i)
        task = Task(
            task_id=task_id,
            dataset="hidden",
            family="refund_request",
            variant="normal",
            input_payload={"customer_id": f"CUS-{i}", "customer_message": f"Help {i}"},
            world_state_seed=world,
            ground_truth={"expected_resolution": "refund", "must_escalate": False},
        )
        db_session.add(task)

    # Set hidden_task_count to 3 for testing
    await settings_service.set("hidden_task_count", 3)
    await settings_service.set("submission_limit_per_team", 2)
    await db_session.commit()

    return team, token


@pytest.mark.asyncio
async def test_submission_start_success_and_monotonic_attempt(
    client: AsyncClient, db_session: AsyncSession, registered_team
):
    team, token = registered_team
    headers = {"Authorization": f"Bearer {token}"}

    # 1. Start 1st submission
    resp = await client.post("/submission/start", headers=headers)
    assert resp.status_code == 200
    data = resp.json()
    assert data["attempt_number"] == 1
    assert data["tasks_total"] == 3
    assert "submission_id" in data
    sub_id_1 = data["submission_id"]

    # Verify status is in_progress
    st_resp = await client.get(f"/submission/{sub_id_1}/status", headers=headers)
    assert st_resp.status_code == 200
    st_data = st_resp.json()
    assert st_data["status"] == "in_progress"
    assert st_data["tasks_completed"] == 0
    assert st_data["tasks_total"] == 3
    assert st_data["time_remaining_seconds"] > 0

    # 2. Cannot start 2nd submission while 1st is in_progress
    resp_conflict = await client.post("/submission/start", headers=headers)
    assert resp_conflict.status_code == 409
    assert resp_conflict.json()["detail"]["error"] == "ACTIVE_SUBMISSION_EXISTS"

    # 3. Finalize 1st submission
    fin_resp = await client.post(f"/submission/{sub_id_1}/finalize", headers=headers)
    assert fin_resp.status_code == 200
    assert fin_resp.json()["status"] == "completed"

    # 4. Start 2nd submission -> attempt_number must be 2
    resp_2 = await client.post("/submission/start", headers=headers)
    assert resp_2.status_code == 200
    assert resp_2.json()["attempt_number"] == 2
    sub_id_2 = resp_2.json()["submission_id"]

    # Finalize 2nd submission
    await client.post(f"/submission/{sub_id_2}/finalize", headers=headers)

    # 5. Attempt 3rd submission -> limit of 2 reached -> 403 SUBMISSION_LIMIT_EXCEEDED
    resp_3 = await client.post("/submission/start", headers=headers)
    assert resp_3.status_code == 403
    assert resp_3.json()["detail"]["error"] == "SUBMISSION_LIMIT_EXCEEDED"


@pytest.mark.asyncio
async def test_submission_finalize_idempotent_and_closed_state(
    client: AsyncClient, db_session: AsyncSession, registered_team
):
    team, token = registered_team
    headers = {"Authorization": f"Bearer {token}"}

    # Start submission
    resp = await client.post("/submission/start", headers=headers)
    assert resp.status_code == 200
    sub_id = resp.json()["submission_id"]

    # Finalize 1st time
    fin_1 = await client.post(f"/submission/{sub_id}/finalize", headers=headers)
    assert fin_1.status_code == 200
    assert fin_1.json()["status"] == "completed"

    # Finalize 2nd time -> Idempotent observation
    fin_2 = await client.post(f"/submission/{sub_id}/finalize", headers=headers)
    assert fin_2.status_code == 200
    assert fin_2.json()["status"] == "completed"

    # Status shows completed and time_remaining_seconds = 0
    st_resp = await client.get(f"/submission/{sub_id}/status", headers=headers)
    assert st_resp.status_code == 200
    assert st_resp.json()["status"] == "completed"
    assert st_resp.json()["time_remaining_seconds"] == 0

    # Cannot start task on finalized submission
    task_resp = await client.post("/task/start", headers=headers)
    assert task_resp.status_code == 409
    assert task_resp.json()["detail"]["error"] == "SUBMISSION_ALREADY_FINALIZED"


@pytest.mark.asyncio
async def test_submission_idor_protection(client: AsyncClient, db_session: AsyncSession, registered_team):
    """Verifies that Team A cannot query or finalize Team B's submission."""
    team_a, token_a = registered_team

    # Register Team B
    team_b_id = uuid.uuid4()
    token_b = create_bearer_token(team_b_id, token_version=1)
    team_b = Team(
        team_id=team_b_id,
        team_name="TeamB",
        bearer_token_hash=hash_token(token_b),
        status="active",
    )
    db_session.add(team_b)
    await db_session.commit()

    headers_a = {"Authorization": f"Bearer {token_a}"}
    headers_b = {"Authorization": f"Bearer {token_b}"}

    # Team B starts a submission
    resp_b = await client.post("/submission/start", headers=headers_b)
    assert resp_b.status_code == 200
    sub_id_b = resp_b.json()["submission_id"]

    # Team A attempts GET /submission/{sub_id_b}/status -> 404 NOT_FOUND (no leak)
    idor_status = await client.get(f"/submission/{sub_id_b}/status", headers=headers_a)
    assert idor_status.status_code == 404
    assert idor_status.json()["detail"]["error"] == "SUBMISSION_NOT_FOUND"

    # Team A attempts POST /submission/{sub_id_b}/finalize -> 404 NOT_FOUND
    idor_fin = await client.post(f"/submission/{sub_id_b}/finalize", headers=headers_a)
    assert idor_fin.status_code == 404
    assert idor_fin.json()["detail"]["error"] == "SUBMISSION_NOT_FOUND"

    # Team B's submission remains in_progress
    check_b = await client.get(f"/submission/{sub_id_b}/status", headers=headers_b)
    assert check_b.status_code == 200
    assert check_b.json()["status"] == "in_progress"
