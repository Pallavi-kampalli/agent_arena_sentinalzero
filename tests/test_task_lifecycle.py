import copy
import uuid

import pytest
from conftest import generate_world
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from agent_arena.models.task import Task
from agent_arena.models.team import Team
from agent_arena.services.auth_service import create_bearer_token, hash_token
from agent_arena.services.settings_service import SettingsService


@pytest.fixture
async def registered_team_and_tasks(db_session: AsyncSession):
    settings_service = SettingsService(db_session)
    await settings_service.seed_defaults()

    team_id = uuid.uuid4()
    token = create_bearer_token(team_id, token_version=1)
    token_hash = hash_token(token)

    team = Team(
        team_id=team_id,
        team_name="TaskLifecycleTeam",
        members=[{"name": "Tester", "email": "test@test.org"}],
        bearer_token_hash=token_hash,
        token_version=1,
        status="active",
    )
    db_session.add(team)

    # Seed 3 hidden tasks
    for i in range(3):
        task_id = f"TASK-HIDDEN-{i:03d}"
        world = generate_world(seed=3000 + i)
        task = Task(
            task_id=task_id,
            dataset="hidden",
            input_payload={"customer_id": f"CUS-{i:03d}", "customer_message": f"Customer issue {i}"},
            world_state_seed=world,
            ground_truth={"expected_resolution": "refund", "must_escalate": False, "required_evidence": ["DOC-1001"]},
        )
        db_session.add(task)

    await settings_service.set("hidden_task_count", 2)  # 2 tasks per submission
    await settings_service.set("time_budget_per_task_seconds", 180)
    await db_session.commit()

    return team, token


@pytest.mark.asyncio
async def test_task_start_without_submission_rejected(
    client: AsyncClient, db_session: AsyncSession, registered_team_and_tasks
):
    team, token = registered_team_and_tasks
    headers = {"Authorization": f"Bearer {token}"}

    # Attempt to start task without active submission
    resp = await client.post("/task/start", headers=headers)
    assert resp.status_code == 404
    assert resp.json()["detail"]["error"] == "NO_ACTIVE_SUBMISSION"


@pytest.mark.asyncio
async def test_task_lifecycle_flow_and_zero_oracle_leakage(
    client: AsyncClient, db_session: AsyncSession, registered_team_and_tasks
):
    team, token = registered_team_and_tasks
    headers = {"Authorization": f"Bearer {token}"}

    # 1. Start submission
    sub_resp = await client.post("/submission/start", headers=headers)
    assert sub_resp.status_code == 200
    sub_id = sub_resp.json()["submission_id"]

    # 2. Start task 1
    t1_resp = await client.post("/task/start", headers=headers)
    assert t1_resp.status_code == 200
    t1_data = t1_resp.json()
    assert t1_data["task_id"] == "TASK-HIDDEN-000"
    assert t1_data["customer_id"] == "CUS-000"
    assert t1_data["customer_message"] == "Customer issue 0"
    # Zero ground truth leakage
    for forbidden in ("ground_truth", "expected_resolution", "expected_evidence", "must_escalate", "world_state"):
        assert forbidden not in t1_data

    # 3. Cannot start task 2 while task 1 is in progress
    t2_blocked = await client.post("/task/start", headers=headers)
    assert t2_blocked.status_code == 409
    assert t2_blocked.json()["detail"]["error"] == "TASK_IN_PROGRESS"

    # 4. Submit task 1 with valid schema
    submit_payload = {
        "task_id": "TASK-HIDDEN-000",
        "case_classification": {
            "category": "billing",
            "issue": "refund_request",
            "severity": "medium",
        },
        "decision": {
            "resolution": "refund",
            "escalation_required": False,
        },
        "evidence": ["TXN-1001", "DOC-1001"],
        "uncertainties": [],
        "customer_response": "We have processed your refund per policy.",
        "confidence": 0.95,
    }
    sub_res = await client.post("/task/submit", json=submit_payload, headers=headers)
    assert sub_res.status_code == 200
    assert sub_res.json() == {"received": True, "task_id": "TASK-HIDDEN-000"}

    # Zero oracle leakage in production submit response
    for forbidden in ("correct", "expected_resolution", "expected_evidence", "score", "diff_explanation"):
        assert forbidden not in sub_res.json()

    # 5. Duplicate submit on task 1 is rejected
    dupe_sub = await client.post("/task/submit", json=submit_payload, headers=headers)
    assert dupe_sub.status_code in (404, 409)

    # 6. Check submission status: tasks_completed == 1
    st = await client.get(f"/submission/{sub_id}/status", headers=headers)
    assert st.status_code == 200
    assert st.json()["tasks_completed"] == 1

    # 7. Start task 2
    t2_resp = await client.post("/task/start", headers=headers)
    assert t2_resp.status_code == 200
    assert t2_resp.json()["task_id"] == "TASK-HIDDEN-001"

    # Submit task 2
    submit_payload_2 = copy.deepcopy(submit_payload)
    submit_payload_2["task_id"] = "TASK-HIDDEN-001"
    sub_res_2 = await client.post("/task/submit", json=submit_payload_2, headers=headers)
    assert sub_res_2.status_code == 200

    # 8. All tasks completed (hidden_task_count = 2) -> next task start fails
    t3_resp = await client.post("/task/start", headers=headers)
    assert t3_resp.status_code == 400
    assert t3_resp.json()["detail"]["error"] == "ALL_TASKS_COMPLETED"

    # 9. Finalize submission
    fin = await client.post(f"/submission/{sub_id}/finalize", headers=headers)
    assert fin.status_code == 200
    assert fin.json()["status"] == "completed"


@pytest.mark.asyncio
async def test_cross_team_task_submit_spoofing(
    client: AsyncClient, db_session: AsyncSession, registered_team_and_tasks
):
    team_a, token_a = registered_team_and_tasks

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

    # Team B starts submission and starts Task 0
    await client.post("/submission/start", headers=headers_b)
    t_b = await client.post("/task/start", headers=headers_b)
    task_b_id = t_b.json()["task_id"]

    # 1. Team A has no active task -> attempts to submit Team B's task_id -> rejected
    payload = {
        "task_id": task_b_id,
        "case_classification": {"category": "billing", "issue": "refund", "severity": "low"},
        "decision": {"resolution": "refund", "escalation_required": False},
        "evidence": ["DOC-1001"],
        "customer_response": "Team A spoof attempt",
        "confidence": 0.9,
    }
    spoof_res = await client.post("/task/submit", json=payload, headers=headers_a)
    assert spoof_res.status_code == 404
    assert spoof_res.json()["detail"]["error"] == "NO_ACTIVE_SUBMISSION"

    # 2. Team B completes task 0 and advances to task 1
    sub_b = await client.post("/task/submit", json=payload, headers=headers_b)
    assert sub_b.status_code == 200
    t_b2 = await client.post("/task/start", headers=headers_b)
    assert t_b2.status_code == 200
    task_b2_id = t_b2.json()["task_id"]

    # Team A starts submission and starts its own first task (TASK-HIDDEN-000)
    await client.post("/submission/start", headers=headers_a)
    t_a = await client.post("/task/start", headers=headers_a)
    assert t_a.status_code == 200
    assert t_a.json()["task_id"] != task_b2_id

    # Team A attempts to submit Team B's active task (TASK-HIDDEN-001)
    spoof_payload = copy.deepcopy(payload)
    spoof_payload["task_id"] = task_b2_id
    spoof_res2 = await client.post("/task/submit", json=spoof_payload, headers=headers_a)
    assert spoof_res2.status_code == 404
    assert spoof_res2.json()["detail"]["error"] == "TASK_NOT_FOUND"
    assert "not the currently active task" in spoof_res2.json()["detail"]["message"]


@pytest.mark.asyncio
async def test_input_validation_attacks(client: AsyncClient, db_session: AsyncSession, registered_team_and_tasks):
    team, token = registered_team_and_tasks
    headers = {"Authorization": f"Bearer {token}"}

    await client.post("/submission/start", headers=headers)
    t = await client.post("/task/start", headers=headers)
    task_id = t.json()["task_id"]

    valid_base = {
        "task_id": task_id,
        "case_classification": {"category": "billing", "issue": "refund", "severity": "medium"},
        "decision": {"resolution": "refund", "escalation_required": False},
        "evidence": ["DOC-1001"],
        "customer_response": "Valid response",
        "confidence": 0.8,
    }

    # Attack 1: Extra forbidden field
    p1 = copy.deepcopy(valid_base)
    p1["team_id"] = "hacked"
    r1 = await client.post("/task/submit", json=p1, headers=headers)
    assert r1.status_code == 422

    # Attack 2: Confidence > 1.0
    p2 = copy.deepcopy(valid_base)
    p2["confidence"] = 1.5
    r2 = await client.post("/task/submit", json=p2, headers=headers)
    assert r2.status_code == 422

    # Attack 3: Confidence < 0.0
    p3 = copy.deepcopy(valid_base)
    p3["confidence"] = -0.1
    r3 = await client.post("/task/submit", json=p3, headers=headers)
    assert r3.status_code == 422

    # Attack 4: Invalid resolution enum
    p4 = copy.deepcopy(valid_base)
    p4["decision"]["resolution"] = "give_free_money"
    r4 = await client.post("/task/submit", json=p4, headers=headers)
    assert r4.status_code == 422
