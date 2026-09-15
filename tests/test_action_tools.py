import pytest
from httpx import AsyncClient
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from agent_arena.models.task import Task
from agent_arena.models.task_assignment import TaskAssignment
from agent_arena.models.tool_call_log import ToolCallLog
from agent_arena.services.auth_service import register_team
from agent_arena.services.settings_service import SettingsService
from agent_arena.services.tool_service import ToolService
from agent_arena.world.generator import generate_world


@pytest.fixture
async def setup_action_world(db_session: AsyncSession):
    """Sets up a team and assigned task with specific test transactions and subscriptions."""
    settings_service = SettingsService(db_session)
    await settings_service.seed_defaults()

    team, token = await register_team(
        session=db_session,
        team_name="ActionTesters",
        members=[{"name": "Carol"}],
    )

    world = generate_world(seed=42)

    # Inject specific controlled test fixtures into world state
    world["customers"].append({
        "id": "CUS-ACTION-01",
        "name": "Action User",
        "tier": "pro",
        "region": "NA",
        "verification_status": "verified",
        "account_status": "active",
        "created_at": "2026-01-01T00:00:00Z",
    })

    # 1. Eligible transaction (3 days ago, amount $100)
    world["transactions"].append({
        "id": "TXN-ELIGIBLE-01",
        "customer_id": "CUS-ACTION-01",
        "amount": 100.0,
        "currency": "USD",
        "date": "2026-09-12T00:00:00Z",
        "status": "completed",
        "chargeback_status": "none",
        "under_fraud_investigation": False,
        "refund_status": "none",
        "refunded_amount": 0.0,
    })

    # 2. Ineligible transaction (chargeback hold active)
    world["transactions"].append({
        "id": "TXN-HOLD-01",
        "customer_id": "CUS-ACTION-01",
        "amount": 250.0,
        "currency": "USD",
        "date": "2026-09-10T00:00:00Z",
        "status": "completed",
        "chargeback_status": "investigation_active",
        "under_fraud_investigation": True,
        "refund_status": "none",
        "refunded_amount": 0.0,
    })

    # 3. Ineligible transaction (outside 30-day window: 60 days ago)
    world["transactions"].append({
        "id": "TXN-EXPIRED-01",
        "customer_id": "CUS-ACTION-01",
        "amount": 50.0,
        "currency": "USD",
        "date": "2026-07-01T00:00:00Z",
        "status": "completed",
        "chargeback_status": "none",
        "under_fraud_investigation": False,
        "refund_status": "none",
        "refunded_amount": 0.0,
    })

    # 4. Eligible subscription (monthly plan, no lock-in)
    world["subscriptions"].append({
        "id": "SUB-MONTHLY-01",
        "customer_id": "CUS-ACTION-01",
        "plan": "pro_monthly",
        "billing_cycle": "monthly",
        "amount": 99.0,
        "status": "active",
        "start_date": "2026-08-01T00:00:00Z",
        "lock_in_until": None,
        "has_approved_exception": False,
        "has_unresolved_dispute": False,
    })

    # 5. Ineligible subscription (annual plan in lock-in without exception)
    world["subscriptions"].append({
        "id": "SUB-LOCKIN-01",
        "customer_id": "CUS-ACTION-01",
        "plan": "pro_annual",
        "billing_cycle": "annual",
        "amount": 990.0,
        "status": "active",
        "start_date": "2026-03-01T00:00:00Z",
        "lock_in_until": "2027-03-01T00:00:00Z",
        "has_approved_exception": False,
        "has_unresolved_dispute": False,
    })

    task = Task(
        task_id="TASK-ACTION-001",
        dataset="dev",
        family="refund_request",
        variant="normal",
        input_payload={"customer_id": "CUS-ACTION-01", "customer_message": "Action test"},
        world_state_seed=world,
        ground_truth={"expected_resolution": "refund", "must_escalate": False, "required_evidence": ["DOC-1001"]},
    )
    db_session.add(task)
    await db_session.commit()

    tool_service = ToolService(db_session, settings_service)
    assignment = await tool_service.assign_task(team.team_id, "TASK-ACTION-001")

    return {
        "team": team,
        "token": token,
        "task": task,
        "assignment": assignment,
    }


# =============================================================================
# Issue Refund Tests
# =============================================================================

@pytest.mark.asyncio
async def test_issue_refund_success_and_state_mutation(client: AsyncClient, setup_action_world, db_session: AsyncSession):
    """Verify eligible refund mutates world state, records tool log, and returns HTTP 200 refunded."""
    token = setup_action_world["token"]
    assignment_id = setup_action_world["assignment"].id
    headers = {"Authorization": f"Bearer {token}"}

    resp = await client.post(
        "/tools/issue_refund",
        json={"transaction_id": "TXN-ELIGIBLE-01", "amount": 100.0, "reason": "Customer request verified"},
        headers=headers,
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "refunded"
    assert data["transaction"]["id"] == "TXN-ELIGIBLE-01"
    assert data["transaction"]["refund_status"] == "refunded"

    # Verify state in database was mutated
    db_session.expire_all()
    assign_db = await db_session.get(TaskAssignment, assignment_id)
    tx = next(t for t in assign_db.world_runtime_state["transactions"] if t["id"] == "TXN-ELIGIBLE-01")
    assert tx["refund_status"] == "refunded"
    assert tx["refunded_amount"] == 100.0

    # Verify tool log recorded
    log = (await db_session.execute(
        sa.select(ToolCallLog).where(ToolCallLog.tool_name == "issue_refund")
    )).scalar_one()
    assert log.was_enforcement_rejection is False
    assert log.latency_ms > 0
    assert "Bearer" not in str(log.request_payload)


@pytest.mark.asyncio
async def test_issue_refund_already_refunded_rejected(client: AsyncClient, setup_action_world, db_session: AsyncSession):
    """Verify refunding an already refunded transaction returns HTTP 200 INELIGIBLE with zero state change."""
    token = setup_action_world["token"]
    headers = {"Authorization": f"Bearer {token}"}

    # 1. First refund succeeds
    resp1 = await client.post(
        "/tools/issue_refund",
        json={"transaction_id": "TXN-ELIGIBLE-01", "amount": 100.0, "reason": "First refund"},
        headers=headers,
    )
    assert resp1.status_code == 200
    assert resp1.json()["status"] == "refunded"

    # 2. Second refund rejected by policy
    resp2 = await client.post(
        "/tools/issue_refund",
        json={"transaction_id": "TXN-ELIGIBLE-01", "amount": 100.0, "reason": "Duplicate second refund attempt"},
        headers=headers,
    )
    assert resp2.status_code == 200
    data = resp2.json()
    assert data["error"] == "INELIGIBLE"
    assert data["reason"] == "already_refunded"
    assert data["policy_ref"] == "DOC-1001"

    # Verify log marked as enforcement rejection
    logs = (await db_session.execute(
        sa.select(ToolCallLog)
        .where(ToolCallLog.tool_name == "issue_refund")
        .order_by(ToolCallLog.created_at.desc())
    )).scalars().all()
    latest_log = logs[0]
    assert latest_log.was_enforcement_rejection is True


@pytest.mark.asyncio
async def test_issue_refund_chargeback_hold_rejected(client: AsyncClient, setup_action_world, db_session: AsyncSession):
    """Verify refund on transaction with active chargeback hold returns INELIGIBLE DOC-1842 and leaves state unchanged."""
    token = setup_action_world["token"]
    assignment_id = setup_action_world["assignment"].id
    headers = {"Authorization": f"Bearer {token}"}

    resp = await client.post(
        "/tools/issue_refund",
        json={"transaction_id": "TXN-HOLD-01", "amount": 250.0, "reason": "Customer demands refund"},
        headers=headers,
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["error"] == "INELIGIBLE"
    assert data["reason"] == "chargeback_investigation_active"
    assert data["policy_ref"] == "DOC-1842"

    # Verify state was NOT mutated
    db_session.expire_all()
    assign_db = await db_session.get(TaskAssignment, assignment_id)
    tx = next(t for t in assign_db.world_runtime_state["transactions"] if t["id"] == "TXN-HOLD-01")
    assert tx["refund_status"] == "none"
    assert tx["refunded_amount"] == 0.0


@pytest.mark.asyncio
async def test_issue_refund_outside_window_rejected(client: AsyncClient, setup_action_world):
    """Verify transaction older than 30 days is rejected with outside_refund_window."""
    token = setup_action_world["token"]
    headers = {"Authorization": f"Bearer {token}"}

    resp = await client.post(
        "/tools/issue_refund",
        json={"transaction_id": "TXN-EXPIRED-01", "amount": 50.0, "reason": "Old transaction"},
        headers=headers,
    )
    assert resp.status_code == 200
    assert resp.json()["error"] == "INELIGIBLE"
    assert resp.json()["reason"] == "outside_refund_window"


@pytest.mark.asyncio
async def test_issue_refund_unknown_transaction_returns_404(client: AsyncClient, setup_action_world):
    """Verify non-existent transaction returns HTTP 404."""
    token = setup_action_world["token"]
    headers = {"Authorization": f"Bearer {token}"}

    resp = await client.post(
        "/tools/issue_refund",
        json={"transaction_id": "TXN-NONEXISTENT-999", "amount": 50.0, "reason": "Ghost transaction"},
        headers=headers,
    )
    assert resp.status_code == 404
    assert resp.json()["detail"]["error"] == "TRANSACTION_NOT_FOUND"


# =============================================================================
# Cancel Subscription Tests
# =============================================================================

@pytest.mark.asyncio
async def test_cancel_subscription_success_and_lock_in_rejection(client: AsyncClient, setup_action_world, db_session: AsyncSession):
    """Verify monthly plan cancels successfully, while annual in lock-in is rejected."""
    token = setup_action_world["token"]
    headers = {"Authorization": f"Bearer {token}"}

    # 1. Eligible monthly plan
    resp_eligible = await client.post(
        "/tools/cancel_subscription",
        json={"customer_id": "CUS-ACTION-01", "subscription_id": "SUB-MONTHLY-01"},
        headers=headers,
    )
    assert resp_eligible.status_code == 200
    assert resp_eligible.json()["status"] == "cancelled"

    # 2. Ineligible annual plan in lock-in without approved exception
    resp_lockin = await client.post(
        "/tools/cancel_subscription",
        json={"customer_id": "CUS-ACTION-01", "subscription_id": "SUB-LOCKIN-01"},
        headers=headers,
    )
    assert resp_lockin.status_code == 200
    assert resp_lockin.json()["error"] == "INELIGIBLE"
    assert resp_lockin.json()["reason"] == "lock_in_period_active"
    assert resp_lockin.json()["policy_ref"] == "DOC-1003"


# =============================================================================
# Escalate Case Tests (Evidence Grounding)
# =============================================================================

@pytest.mark.asyncio
async def test_escalate_case_grounded_vs_ungrounded(client: AsyncClient, setup_action_world):
    """Verify escalation succeeds ONLY when reason cites evidence actually retrieved by team."""
    token = setup_action_world["token"]
    headers = {"Authorization": f"Bearer {token}"}

    # 1. Attempt ungrounded escalation (reason doesn't cite any retrieved ID)
    resp_ungrounded = await client.post(
        "/tools/escalate_case",
        json={"case_id": "CASE-01", "team": "billing_specialists", "reason": "Customer is mad and wants money"},
        headers=headers,
    )
    assert resp_ungrounded.status_code == 200
    assert resp_ungrounded.json()["error"] == "INVALID_ESCALATION"
    assert resp_ungrounded.json()["reason"] == "reason_not_grounded"

    # 2. Team retrieves document DOC-1842
    doc_resp = await client.post("/tools/get_document", json={"document_id": "DOC-1842"}, headers=headers)
    assert doc_resp.status_code == 200

    # 3. Now escalate citing the retrieved DOC-1842
    resp_grounded = await client.post(
        "/tools/escalate_case",
        json={
            "case_id": "CASE-01",
            "team": "billing_specialists",
            "reason": "Active chargeback hold requires escalation per retrieved policy DOC-1842",
        },
        headers=headers,
    )
    assert resp_grounded.status_code == 200
    assert resp_grounded.json()["status"] == "escalated"


# =============================================================================
# Request Verification Tests (Safe Fallback)
# =============================================================================

@pytest.mark.asyncio
async def test_request_verification_safe_fallback(client: AsyncClient, setup_action_world, db_session: AsyncSession):
    """Verify request_verification always succeeds for existing customer and records challenge."""
    token = setup_action_world["token"]
    headers = {"Authorization": f"Bearer {token}"}

    resp = await client.post(
        "/tools/request_verification",
        json={"customer_id": "CUS-ACTION-01", "verification_type": "sms_otp"},
        headers=headers,
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "verification_requested"
