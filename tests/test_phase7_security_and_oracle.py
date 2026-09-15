from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from agent_arena.models.task import Task
from agent_arena.models.team import Team
from agent_arena.models.tool_call_log import ToolCallLog
from agent_arena.services.auth_service import register_team
from agent_arena.services.settings_service import SettingsService
from agent_arena.world.generator import generate_world

FORBIDDEN_GT_KEYS = {
    "ground_truth",
    "ground_truth_privileged",
    "expected_resolution",
    "must_escalate",
    "required_evidence",
    "expected_action",
    "expected_end_state",
    "diff_explanation",
    "world_state_seed",
}


def assert_no_ground_truth_leakage(data: Any, context: str):
    """Recursively audits any serialized response, log, or string for forbidden ground-truth keys."""
    if isinstance(data, dict):
        for k, v in data.items():
            assert k not in FORBIDDEN_GT_KEYS, f"Ground-truth key '{k}' leaked in {context}: {data}"
            assert_no_ground_truth_leakage(v, f"{context}->{k}")
    elif isinstance(data, list):
        for idx, item in enumerate(data):
            assert_no_ground_truth_leakage(item, f"{context}[{idx}]")
    elif isinstance(data, str):
        for key in FORBIDDEN_GT_KEYS:
            assert f'"{key}"' not in data, f"Ground-truth key '{key}' found in string ({context}): {data}"


@pytest.fixture
async def seeded_dataset(db_session: AsyncSession):
    settings = SettingsService(db_session)
    await settings.seed_defaults()
    for i in range(5):
        t_id = f"TASK-SEC-{i:03d}"
        world = generate_world(seed=7000 + i)
        cust_id = world["customers"][0]["id"]
        task = Task(
            task_id=t_id,
            dataset="hidden",
            family="refund_request",
            variant="normal",
            input_payload={"customer_id": cust_id, "customer_message": "Need refund urgently"},
            world_state_seed=world,
            ground_truth={"expected_resolution": "refund", "must_escalate": False, "required_evidence": ["DOC-1"]},
        )
        db_session.add(task)
    await settings.set("hidden_task_count", 5)
    await db_session.commit()


@pytest.mark.asyncio
async def test_cross_team_idor_submission_access(client: AsyncClient, seeded_dataset, db_session: AsyncSession):
    """Verifies that Team B cannot read or finalize Team A's submission."""
    team_a, token_a = await register_team(db_session, "TeamSecA")
    team_b, token_b = await register_team(db_session, "TeamSecB")

    headers_a = {"Authorization": f"Bearer {token_a}"}
    headers_b = {"Authorization": f"Bearer {token_b}"}

    # Team A starts submission
    start_resp = await client.post("/submission/start", headers=headers_a)
    assert start_resp.status_code == 200
    sub_id = start_resp.json()["submission_id"]

    # Team B attempts IDOR read of Team A's submission status
    status_resp = await client.get(f"/submission/{sub_id}/status", headers=headers_b)
    assert status_resp.status_code == 404
    assert status_resp.json()["detail"]["error"] == "SUBMISSION_NOT_FOUND"

    # Team B attempts IDOR finalize of Team A's submission
    finalize_resp = await client.post(f"/submission/{sub_id}/finalize", headers=headers_b)
    assert finalize_resp.status_code == 404
    assert finalize_resp.json()["detail"]["error"] == "SUBMISSION_NOT_FOUND"


@pytest.mark.asyncio
async def test_cross_team_idor_task_submit(client: AsyncClient, seeded_dataset, db_session: AsyncSession):
    """Verifies that Team B cannot submit against an unassigned task or Team A's task."""
    team_a, token_a = await register_team(db_session, "TeamTaskSecA")
    team_b, token_b = await register_team(db_session, "TeamTaskSecB")

    headers_a = {"Authorization": f"Bearer {token_a}"}
    headers_b = {"Authorization": f"Bearer {token_b}"}

    # Team A starts submission and advances to second task
    await client.post("/submission/start", headers=headers_a)
    task_resp_a1 = await client.post("/task/start", headers=headers_a)
    assert task_resp_a1.status_code == 200
    t_id_a1 = task_resp_a1.json()["task_id"]

    # Complete task 1 for Team A
    await client.post(
        "/task/submit",
        json={
            "task_id": t_id_a1,
            "case_classification": {"category": "billing", "issue": "refund", "severity": "medium"},
            "decision": {"resolution": "refund", "escalation_required": False},
            "evidence": [],
            "uncertainties": [],
            "customer_response": "done",
            "confidence": 0.9,
        },
        headers=headers_a,
    )

    task_resp_a2 = await client.post("/task/start", headers=headers_a)
    assert task_resp_a2.status_code == 200
    task_id_a2 = task_resp_a2.json()["task_id"]

    # Team B starts submission and gets task 1
    await client.post("/submission/start", headers=headers_b)
    task_resp_b = await client.post("/task/start", headers=headers_b)
    assert task_resp_b.status_code == 200
    task_id_b = task_resp_b.json()["task_id"]

    # Team B tries to submit Team A's current active task (task_id_a2)
    assert task_id_b != task_id_a2
    submit_payload = {
        "task_id": task_id_a2,
        "case_classification": {"category": "billing", "issue": "refund", "severity": "medium"},
        "decision": {"resolution": "refund", "escalation_required": False},
        "evidence": [],
        "uncertainties": [],
        "customer_response": "Processed refund",
        "confidence": 0.95,
    }
    submit_resp = await client.post("/task/submit", json=submit_payload, headers=headers_b)
    assert submit_resp.status_code == 404
    assert submit_resp.json()["detail"]["error"] == "TASK_NOT_FOUND"


@pytest.mark.asyncio
async def test_zero_oracle_leakage_participant_stack(client: AsyncClient, seeded_dataset, db_session: AsyncSession):
    """Audits all participant endpoints to prove ZERO ground-truth or world-seed keys leak."""
    team, token = await register_team(db_session, "OracleAuditTeam")
    headers = {"Authorization": f"Bearer {token}"}

    # 1. /submission/start
    resp_sub_start = await client.post("/submission/start", headers=headers)
    assert resp_sub_start.status_code == 200
    sub_data = resp_sub_start.json()
    assert_no_ground_truth_leakage(sub_data, "POST /submission/start")
    sub_id = sub_data["submission_id"]

    # 2. /task/start
    resp_task_start = await client.post("/task/start", headers=headers)
    assert resp_task_start.status_code == 200
    task_data = resp_task_start.json()
    assert_no_ground_truth_leakage(task_data, "POST /task/start")
    task_id = task_data["task_id"]
    customer_id = task_data["customer_id"]

    # 3. Read tools
    resp_customer = await client.post("/tools/get_customer", json={"customer_id": customer_id}, headers=headers)
    assert resp_customer.status_code == 200
    assert_no_ground_truth_leakage(resp_customer.json(), "POST /tools/get_customer")

    resp_search = await client.post(
        "/tools/search_knowledge", json={"query": "refund policy", "top_k": 3}, headers=headers
    )
    assert resp_search.status_code == 200
    assert_no_ground_truth_leakage(resp_search.json(), "POST /tools/search_knowledge")

    resp_txns = await client.post("/tools/get_transactions", json={"customer_id": customer_id}, headers=headers)
    assert resp_txns.status_code == 200
    assert_no_ground_truth_leakage(resp_txns.json(), "POST /tools/get_transactions")

    # 4. /task/submit
    submit_payload = {
        "task_id": task_id,
        "case_classification": {"category": "billing", "issue": "refund", "severity": "medium"},
        "decision": {"resolution": "refund", "escalation_required": False},
        "evidence": [],
        "uncertainties": [],
        "customer_response": "Your refund is processed.",
        "confidence": 0.90,
    }
    resp_submit = await client.post("/task/submit", json=submit_payload, headers=headers)
    assert resp_submit.status_code == 200
    assert_no_ground_truth_leakage(resp_submit.json(), "POST /task/submit")

    # 5. /submission/{id}/status
    resp_status = await client.get(f"/submission/{sub_id}/status", headers=headers)
    assert resp_status.status_code == 200
    assert_no_ground_truth_leakage(resp_status.json(), "GET /submission/status")

    # 6. /submission/{id}/finalize
    resp_finalize = await client.post(f"/submission/{sub_id}/finalize", headers=headers)
    assert resp_finalize.status_code == 200
    assert_no_ground_truth_leakage(resp_finalize.json(), "POST /submission/finalize")


@pytest.mark.asyncio
async def test_token_and_secret_hygiene_in_db_and_logs(client: AsyncClient, seeded_dataset, db_session: AsyncSession):
    """Verifies that plaintext tokens are never stored in DB or logged in tool call payloads."""
    # 1. Model hygiene
    assert not hasattr(Team, "bearer_token"), "Team model must never have a plaintext bearer_token column!"
    assert hasattr(Team, "bearer_token_hash"), "Team model must store bearer_token_hash."

    # 2. Tool call log hygiene
    team, token = await register_team(db_session, "LogHygieneTeam")
    headers = {"Authorization": f"Bearer {token}"}
    await client.post("/submission/start", headers=headers)
    task_res = await client.post("/task/start", headers=headers)
    cust_id = task_res.json()["customer_id"]

    # Execute tool call
    await client.post("/tools/get_customer", json={"customer_id": cust_id}, headers=headers)

    # Inspect tool call log in database
    team_id = team.team_id
    db_session.expire_all()
    logs = list(
        (await db_session.execute(ToolCallLog.__table__.select().where(ToolCallLog.team_id == team_id)))
        .mappings()
        .all()
    )
    assert len(logs) >= 1
    for log in logs:
        req_str = str(log["request_payload"])
        resp_str = str(log["response_payload"])
        assert token not in req_str, "Plaintext bearer token found in ToolCallLog.request_payload"
        assert token not in resp_str, "Plaintext bearer token found in ToolCallLog.response_payload"

    # 3. Auth rejection hygiene: 401 responses do not echo secret or tokens
    bad_resp = await client.get("/submission/start", headers={"Authorization": "Bearer leaked-test-token-12345"})
    assert bad_resp.status_code == 401
    assert "leaked-test-token-12345" not in bad_resp.text


@pytest.mark.asyncio
async def test_cross_team_tool_execution_rejection(client: AsyncClient, seeded_dataset, db_session: AsyncSession):
    """Verifies that tool execution fails if the team has no active task assignment."""
    _team_a, token_a = await register_team(db_session, "TeamToolSecA")
    _team_b, token_b = await register_team(db_session, "TeamToolSecB")

    headers_a = {"Authorization": f"Bearer {token_a}"}
    headers_b = {"Authorization": f"Bearer {token_b}"}

    # Team A starts submission and task
    await client.post("/submission/start", headers=headers_a)
    task_res = await client.post("/task/start", headers=headers_a)
    cust_id = task_res.json()["customer_id"]

    # Team A can call tools
    res_a = await client.post("/tools/get_customer", json={"customer_id": cust_id}, headers=headers_a)
    assert res_a.status_code == 200

    # Team B has NOT started a task. Tool call must fail with 404 NO_ACTIVE_TASK
    res_b = await client.post("/tools/get_customer", json={"customer_id": cust_id}, headers=headers_b)
    assert res_b.status_code == 404
    assert res_b.json()["detail"]["error"] == "NO_ACTIVE_TASK"
