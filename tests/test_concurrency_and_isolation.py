import asyncio

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from agent_arena.models.task import Task
from agent_arena.models.task_assignment import TaskAssignment
from agent_arena.services.auth_service import register_team
from agent_arena.services.settings_service import SettingsService
from agent_arena.services.tool_service import ToolService
from conftest import generate_world


@pytest.mark.asyncio
async def test_cross_team_isolation(client: AsyncClient, db_session: AsyncSession):
    """Verify Team A and Team B are completely isolated even with identical entity ID strings."""
    settings_service = SettingsService(db_session)
    await settings_service.seed_defaults()

    # 1. Register Team A and Team B
    team_a, token_a = await register_team(db_session, "TeamAlpha")
    team_b, token_b = await register_team(db_session, "TeamBeta")

    # 2. Build world with identical customer and transaction IDs
    world_a = generate_world(seed=42)
    world_a["customers"].append(
        {
            "id": "CUS-SHARED-99",
            "name": "Alpha Customer",
            "tier": "pro",
            "region": "NA",
            "verification_status": "verified",
            "account_status": "active",
            "created_at": "2026-01-01T00:00:00Z",
        }
    )
    world_a["transactions"].append(
        {
            "id": "TXN-SHARED-99",
            "customer_id": "CUS-SHARED-99",
            "amount": 100.0,
            "currency": "USD",
            "date": "2026-09-12T00:00:00Z",
            "status": "completed",
            "chargeback_status": "none",
            "under_fraud_investigation": False,
            "refund_status": "none",
            "refunded_amount": 0.0,
        }
    )

    world_b = generate_world(seed=42)
    world_b["customers"].append(
        {
            "id": "CUS-SHARED-99",
            "name": "Beta Customer",
            "tier": "enterprise",
            "region": "EU",
            "verification_status": "verified",
            "account_status": "active",
            "created_at": "2026-01-01T00:00:00Z",
        }
    )
    world_b["transactions"].append(
        {
            "id": "TXN-SHARED-99",
            "customer_id": "CUS-SHARED-99",
            "amount": 100.0,
            "currency": "USD",
            "date": "2026-09-12T00:00:00Z",
            "status": "completed",
            "chargeback_status": "none",
            "under_fraud_investigation": False,
            "refund_status": "none",
            "refunded_amount": 0.0,
        }
    )

    task_a = Task(
        task_id="TASK-ISO-A",
        dataset="dev",
        family="refund_request",
        variant="normal",
        input_payload={"customer_id": "CUS-SHARED-99", "customer_message": "A"},
        world_state_seed=world_a,
        ground_truth={"expected_resolution": "refund", "must_escalate": False, "required_evidence": ["DOC-1001"]},
    )
    task_b = Task(
        task_id="TASK-ISO-B",
        dataset="dev",
        family="refund_request",
        variant="normal",
        input_payload={"customer_id": "CUS-SHARED-99", "customer_message": "B"},
        world_state_seed=world_b,
        ground_truth={"expected_resolution": "refund", "must_escalate": False, "required_evidence": ["DOC-1001"]},
    )

    db_session.add(task_a)
    db_session.add(task_b)
    await db_session.commit()

    tool_service = ToolService(db_session, settings_service)
    await tool_service.assign_task(team_a.team_id, "TASK-ISO-A")
    assign_b = await tool_service.assign_task(team_b.team_id, "TASK-ISO-B")

    headers_a = {"Authorization": f"Bearer {token_a}"}
    headers_b = {"Authorization": f"Bearer {token_b}"}

    # 3. Team A reads customer CUS-SHARED-99 -> gets "Alpha Customer"
    resp_a_cust = await client.post("/tools/get_customer", json={"customer_id": "CUS-SHARED-99"}, headers=headers_a)
    assert resp_a_cust.status_code == 200
    assert resp_a_cust.json()["customer"]["name"] == "Alpha Customer"

    # Team B reads customer CUS-SHARED-99 -> gets "Beta Customer"
    resp_b_cust = await client.post("/tools/get_customer", json={"customer_id": "CUS-SHARED-99"}, headers=headers_b)
    assert resp_b_cust.status_code == 200
    assert resp_b_cust.json()["customer"]["name"] == "Beta Customer"

    # 4. Team A refunds TXN-SHARED-99
    resp_a_ref = await client.post(
        "/tools/issue_refund",
        json={"transaction_id": "TXN-SHARED-99", "amount": 100.0, "reason": "Team A refund"},
        headers=headers_a,
    )
    assert resp_a_ref.status_code == 200
    assert resp_a_ref.json()["status"] == "refunded"

    # 5. Verify Team B's transaction TXN-SHARED-99 remains UNMUTATED ("none")
    assign_b_id = assign_b.id
    db_session.expire_all()
    assign_b_db = await db_session.get(TaskAssignment, assign_b_id)
    tx_b = next(t for t in assign_b_db.world_runtime_state["transactions"] if t["id"] == "TXN-SHARED-99")
    assert tx_b["refund_status"] == "none"
    assert tx_b["refunded_amount"] == 0.0

    # Team B can still refund its own TXN-SHARED-99
    resp_b_ref = await client.post(
        "/tools/issue_refund",
        json={"transaction_id": "TXN-SHARED-99", "amount": 100.0, "reason": "Team B refund"},
        headers=headers_b,
    )
    assert resp_b_ref.status_code == 200
    assert resp_b_ref.json()["status"] == "refunded"


@pytest.mark.asyncio
async def test_concurrent_refund_attempts(client: AsyncClient, db_session: AsyncSession):
    """Verify concurrent refund requests for the same transaction serialize correctly (no double refunds)."""
    settings_service = SettingsService(db_session)
    await settings_service.seed_defaults()

    team, token = await register_team(db_session, "RaceTeam")
    world = generate_world(seed=42)
    world["customers"].append(
        {
            "id": "CUS-RACE-01",
            "name": "Race User",
            "tier": "pro",
            "region": "NA",
            "verification_status": "verified",
            "account_status": "active",
            "created_at": "2026-01-01T00:00:00Z",
        }
    )
    world["transactions"].append(
        {
            "id": "TXN-RACE-01",
            "customer_id": "CUS-RACE-01",
            "amount": 100.0,
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
        task_id="TASK-RACE-01",
        dataset="dev",
        family="refund_request",
        variant="normal",
        input_payload={"customer_id": "CUS-RACE-01", "customer_message": "Race"},
        world_state_seed=world,
        ground_truth={"expected_resolution": "refund", "must_escalate": False, "required_evidence": ["DOC-1001"]},
    )
    db_session.add(task)
    await db_session.commit()

    tool_service = ToolService(db_session, settings_service)
    assignment = await tool_service.assign_task(team.team_id, "TASK-RACE-01")

    headers = {"Authorization": f"Bearer {token}"}
    req_body = {"transaction_id": "TXN-RACE-01", "amount": 100.0, "reason": "Concurrent race test"}

    # Issue 2 concurrent requests
    resps = await asyncio.gather(
        client.post("/tools/issue_refund", json=req_body, headers=headers),
        client.post("/tools/issue_refund", json=req_body, headers=headers),
    )

    statuses = [r.status_code for r in resps]
    assert all(s == 200 for s in statuses)

    results = [r.json() for r in resps]
    successes = [r for r in results if r.get("status") == "refunded"]
    rejections = [r for r in results if r.get("error") == "INELIGIBLE"]

    # Exactly one refund succeeds, and exactly one is rejected with already_refunded
    assert len(successes) == 1
    assert len(rejections) == 1
    assert rejections[0]["reason"] == "already_refunded"

    # Verify final database state has refunded_amount == 100.0 (not 200.0)
    assignment_id = assignment.id
    db_session.expire_all()
    assign_db = await db_session.get(TaskAssignment, assignment_id)
    tx = next(t for t in assign_db.world_runtime_state["transactions"] if t["id"] == "TXN-RACE-01")
    assert tx["refund_status"] == "refunded"
    assert tx["refunded_amount"] == 100.0


@pytest.mark.asyncio
async def test_dynamic_rate_limiter_setting(client: AsyncClient, db_session: AsyncSession):
    """Verify changing rate_limit_tool_calls_per_min in settings dynamically throttles without code change."""
    settings_service = SettingsService(db_session)
    await settings_service.seed_defaults()

    team, token = await register_team(db_session, "ThrottleTeam")
    world = generate_world(seed=42)
    task = Task(
        task_id="TASK-THROTTLE-01",
        dataset="dev",
        family="refund_request",
        variant="normal",
        input_payload={"customer_id": "CUS-1001", "customer_message": "Hi"},
        world_state_seed=world,
        ground_truth={"expected_resolution": "refund", "must_escalate": False, "required_evidence": ["DOC-1001"]},
    )
    db_session.add(task)
    await db_session.commit()

    tool_service = ToolService(db_session, settings_service)
    await tool_service.assign_task(team.team_id, "TASK-THROTTLE-01")

    # Set rate limit to 2 calls per minute
    await settings_service.set("rate_limit_tool_calls_per_min", 2)

    headers = {"Authorization": f"Bearer {token}"}

    # Call 1: Success
    r1 = await client.post("/tools/get_customer", json={"customer_id": "CUS-1001"}, headers=headers)
    assert r1.status_code == 200

    # Call 2: Success
    r2 = await client.post("/tools/get_customer", json={"customer_id": "CUS-1001"}, headers=headers)
    assert r2.status_code == 200

    # Call 3: Throttled (429 Rate Limit Exceeded)
    r3 = await client.post("/tools/get_customer", json={"customer_id": "CUS-1001"}, headers=headers)
    assert r3.status_code == 429
    assert r3.json()["detail"]["error"] == "RATE_LIMIT_EXCEEDED"

    # Now dynamically increase limit to 10 in settings
    await settings_service.set("rate_limit_tool_calls_per_min", 10)

    # Call 4: Now succeeds immediately without redeployment!
    r4 = await client.post("/tools/get_customer", json={"customer_id": "CUS-1001"}, headers=headers)
    assert r4.status_code == 200


@pytest.mark.asyncio
async def test_per_task_tool_call_budget(client: AsyncClient, db_session: AsyncSession):
    """Verify tool_call_budget_per_task hard cap returns 429 BUDGET_EXCEEDED."""
    settings_service = SettingsService(db_session)
    await settings_service.seed_defaults()

    team, token = await register_team(db_session, "BudgetTeam")
    world = generate_world(seed=42)
    task = Task(
        task_id="TASK-BUDGET-01",
        dataset="dev",
        family="refund_request",
        variant="normal",
        input_payload={"customer_id": "CUS-1001", "customer_message": "Hi"},
        world_state_seed=world,
        ground_truth={"expected_resolution": "refund", "must_escalate": False, "required_evidence": ["DOC-1001"]},
    )
    db_session.add(task)
    await db_session.commit()

    tool_service = ToolService(db_session, settings_service)
    await tool_service.assign_task(team.team_id, "TASK-BUDGET-01")

    # Set budget to 3 calls for this task
    await settings_service.set("tool_call_budget_per_task", 3)
    # Ensure rate limit doesn't interfere
    await settings_service.set("rate_limit_tool_calls_per_min", 100)

    headers = {"Authorization": f"Bearer {token}"}

    # Calls 1, 2, 3 succeed
    for _ in range(3):
        res = await client.post("/tools/get_customer", json={"customer_id": "CUS-1001"}, headers=headers)
        assert res.status_code == 200

    # Call 4: Exceeds budget
    r4 = await client.post("/tools/get_customer", json={"customer_id": "CUS-1001"}, headers=headers)
    assert r4.status_code == 429
    assert r4.json()["detail"]["error"] == "BUDGET_EXCEEDED"
