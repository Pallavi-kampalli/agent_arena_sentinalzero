import asyncio
import copy
from unittest.mock import patch

import pytest
import sqlalchemy as sa
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from agent_arena.models.task import Task
from agent_arena.models.task_assignment import TaskAssignment
from agent_arena.models.team import Team
from agent_arena.models.tool_call_log import ToolCallLog
from agent_arena.services.auth_service import register_team
from agent_arena.services.settings_service import SettingsService
from agent_arena.services.tool_service import ToolService
from conftest import generate_world


@pytest.fixture
async def setup_adversarial_env(db_session: AsyncSession):
    """Sets up a seeded environment for adversarial tests."""
    settings_service = SettingsService(db_session)
    await settings_service.seed_defaults()
    await settings_service.set("rate_limit_tool_calls_per_min", 1000)
    await settings_service.set("tool_call_budget_per_task", 1000)

    team, token = await register_team(db_session, "AdversarialTeam")

    world = generate_world(seed=101)
    world["customers"].append(
        {
            "id": "CUS-ADV-01",
            "name": "Target Customer",
            "tier": "pro",
            "region": "NA",
            "verification_status": "verified",
            "account_status": "active",
            "created_at": "2026-01-01T00:00:00Z",
        }
    )
    world["transactions"].append(
        {
            "id": "TXN-ADV-01",
            "customer_id": "CUS-ADV-01",
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
    world["transactions"].append(
        {
            "id": "TXN-ADV-HOLD",
            "customer_id": "CUS-ADV-01",
            "amount": 200.0,
            "currency": "USD",
            "date": "2026-09-12T00:00:00Z",
            "status": "completed",
            "chargeback_status": "investigation_active",
            "under_fraud_investigation": True,
            "refund_status": "none",
            "refunded_amount": 0.0,
        }
    )
    world["subscriptions"].append(
        {
            "id": "SUB-ADV-LOCKIN",
            "customer_id": "CUS-ADV-01",
            "plan": "pro_annual",
            "billing_cycle": "annual",
            "amount": 1200.0,
            "status": "active",
            "start_date": "2026-01-01T00:00:00Z",
            "lock_in_until": "2026-12-31T23:59:59Z",
            "has_approved_exception": False,
            "has_unresolved_dispute": False,
        }
    )

    task = Task(
        task_id="TASK-ADV-01",
        dataset="dev",
        family="refund_request",
        variant="normal",
        input_payload={"customer_id": "CUS-ADV-01", "customer_message": "Test"},
        world_state_seed=copy.deepcopy(world),
        ground_truth={"expected_resolution": "refund", "must_escalate": False, "required_evidence": ["DOC-1001"]},
    )
    db_session.add(task)
    await db_session.commit()

    tool_service = ToolService(db_session, settings_service)
    assignment = await tool_service.assign_task(team.team_id, "TASK-ADV-01")

    return {
        "team": team,
        "token": token,
        "task": task,
        "assignment": assignment,
        "assignment_id": assignment.id,
        "world": world,
        "settings_service": settings_service,
    }


# 1. Team Spoofing via Body Attacks (Pydantic extra='forbid')
@pytest.mark.asyncio
async def test_team_spoofing_in_body_rejected(client: AsyncClient, setup_adversarial_env):
    env = setup_adversarial_env
    headers = {"Authorization": f"Bearer {env['token']}"}

    # Attempt to inject team_id into read tool
    r1 = await client.post(
        "/tools/get_customer",
        json={"customer_id": "CUS-ADV-01", "team_id": "team_spoofed"},
        headers=headers,
    )
    assert r1.status_code == 422

    # Attempt to inject assignment_id into action tool
    r2 = await client.post(
        "/tools/issue_refund",
        json={
            "transaction_id": "TXN-ADV-01",
            "amount": 50.0,
            "reason": "spoof",
            "assignment_id": "assign_spoofed",
        },
        headers=headers,
    )
    assert r2.status_code == 422

    # Attempt to inject unknown field into search_knowledge
    r3 = await client.post(
        "/tools/search_knowledge",
        json={"query": "refund", "bypass_auth": True},
        headers=headers,
    )
    assert r3.status_code == 422


# 2. Cross-Task Isolation with Identical Entity IDs
@pytest.mark.asyncio
async def test_cross_task_identical_entities_full_isolation(client: AsyncClient, db_session: AsyncSession):
    settings_service = SettingsService(db_session)
    await settings_service.seed_defaults()

    team1, token1 = await register_team(db_session, "IsoTeam1")
    team2, token2 = await register_team(db_session, "IsoTeam2")

    world1 = generate_world(seed=201)
    world1["customers"].append(
        {
            "id": "CUS-SHARED",
            "name": "Team 1 Customer",
            "tier": "pro",
            "region": "NA",
            "verification_status": "verified",
            "account_status": "active",
            "created_at": "2026-01-01T00:00:00Z",
        }
    )
    world1["transactions"].append(
        {
            "id": "TXN-SHARED",
            "customer_id": "CUS-SHARED",
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

    world2 = generate_world(seed=202)
    world2["customers"].append(
        {
            "id": "CUS-SHARED",
            "name": "Team 2 Customer",
            "tier": "free",
            "region": "EU",
            "verification_status": "verified",
            "account_status": "active",
            "created_at": "2026-01-01T00:00:00Z",
        }
    )
    world2["transactions"].append(
        {
            "id": "TXN-SHARED",
            "customer_id": "CUS-SHARED",
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

    task1 = Task(
        task_id="TASK-SHARED-1",
        dataset="dev",
        family="refund_request",
        variant="normal",
        input_payload={"customer_id": "CUS-SHARED", "customer_message": "T1"},
        world_state_seed=world1,
        ground_truth={"expected_resolution": "refund", "must_escalate": False, "required_evidence": ["DOC-1001"]},
    )
    task2 = Task(
        task_id="TASK-SHARED-2",
        dataset="dev",
        family="refund_request",
        variant="normal",
        input_payload={"customer_id": "CUS-SHARED", "customer_message": "T2"},
        world_state_seed=world2,
        ground_truth={"expected_resolution": "refund", "must_escalate": False, "required_evidence": ["DOC-1001"]},
    )
    db_session.add_all([task1, task2])
    await db_session.commit()

    tool_service = ToolService(db_session, settings_service)
    await tool_service.assign_task(team1.team_id, "TASK-SHARED-1")
    await tool_service.assign_task(team2.team_id, "TASK-SHARED-2")

    headers1 = {"Authorization": f"Bearer {token1}"}
    headers2 = {"Authorization": f"Bearer {token2}"}

    # Team 1 refunds $80
    r1 = await client.post(
        "/tools/issue_refund",
        json={"transaction_id": "TXN-SHARED", "amount": 80.0, "reason": "T1 partial"},
        headers=headers1,
    )
    assert r1.status_code == 200
    assert r1.json()["status"] == "partially_refunded"
    assert r1.json()["transaction"]["refunded_amount"] == 80.0

    # Verify Team 2 still has pristine transaction with refunded_amount 0.0
    r2 = await client.post("/tools/get_transactions", json={"customer_id": "CUS-SHARED"}, headers=headers2)
    assert r2.status_code == 200
    tx2 = next(t for t in r2.json()["transactions"] if t["id"] == "TXN-SHARED")
    assert tx2["refund_status"] == "none"
    assert tx2["refunded_amount"] == 0.0

    # Team 2 refunds entire $200
    r3 = await client.post(
        "/tools/issue_refund",
        json={"transaction_id": "TXN-SHARED", "amount": 200.0, "reason": "T2 full"},
        headers=headers2,
    )
    assert r3.status_code == 200
    assert r3.json()["status"] == "refunded"
    assert r3.json()["transaction"]["refunded_amount"] == 200.0

    # Verify Team 1's transaction is still at 80.0
    r4 = await client.post("/tools/get_transactions", json={"customer_id": "CUS-SHARED"}, headers=headers1)
    assert r4.status_code == 200
    tx1 = next(t for t in r4.json()["transactions"] if t["id"] == "TXN-SHARED")
    assert tx1["refund_status"] == "partially_refunded"
    assert tx1["refunded_amount"] == 80.0


# 3. Seed Immutability Test Across All Actions
@pytest.mark.asyncio
async def test_seed_immutability_across_actions(client: AsyncClient, db_session: AsyncSession, setup_adversarial_env):
    env = setup_adversarial_env
    headers = {"Authorization": f"Bearer {env['token']}"}

    # Fetch initial seed snapshot from DB
    db_session.expire_all()
    task_before = await db_session.get(Task, "TASK-ADV-01")
    initial_seed = copy.deepcopy(task_before.world_state_seed)

    # Perform Read tools
    await client.post("/tools/search_knowledge", json={"query": "refund"}, headers=headers)
    await client.post("/tools/get_document", json={"document_id": "DOC-1001"}, headers=headers)
    await client.post("/tools/get_customer", json={"customer_id": "CUS-ADV-01"}, headers=headers)
    await client.post("/tools/get_transactions", json={"customer_id": "CUS-ADV-01"}, headers=headers)
    await client.post("/tools/get_subscription", json={"customer_id": "CUS-ADV-01"}, headers=headers)
    await client.post("/tools/get_previous_cases", json={"customer_id": "CUS-ADV-01"}, headers=headers)

    # Perform Action tools
    await client.post(
        "/tools/issue_refund",
        json={"transaction_id": "TXN-ADV-01", "amount": 20.0, "reason": "action"},
        headers=headers,
    )
    await client.post(
        "/tools/cancel_subscription",
        json={"customer_id": "CUS-ADV-01", "subscription_id": "SUB-ADV-LOCKIN"},
        headers=headers,
    )
    await client.post(
        "/tools/escalate_case",
        json={"case_id": "TXN-ADV-01", "team": "billing_specialists", "reason": "Escalating per DOC-1001"},
        headers=headers,
    )
    await client.post(
        "/tools/request_verification",
        json={"customer_id": "CUS-ADV-01", "verification_type": "identity"},
        headers=headers,
    )

    # Re-fetch task and verify world_state_seed is 100% byte-for-byte identical
    db_session.expire_all()
    task_after = await db_session.get(Task, "TASK-ADV-01")
    assert task_after.world_state_seed == initial_seed


# 4. Zero Mutation on Rejections
@pytest.mark.asyncio
async def test_zero_mutation_on_rejected_actions(client: AsyncClient, db_session: AsyncSession, setup_adversarial_env):
    env = setup_adversarial_env
    headers = {"Authorization": f"Bearer {env['token']}"}
    assign_id = env["assignment_id"]

    # Snapshot current runtime state
    assign = await db_session.get(TaskAssignment, assign_id)
    snapshot_before = copy.deepcopy(assign.world_runtime_state)

    # Attempt 1: Ineligible refund due to chargeback hold
    r1 = await client.post(
        "/tools/issue_refund",
        json={"transaction_id": "TXN-ADV-HOLD", "amount": 50.0, "reason": "Hold bypass attempt"},
        headers=headers,
    )
    assert r1.status_code == 200
    assert r1.json()["error"] == "INELIGIBLE"
    assert r1.json()["reason"] == "chargeback_investigation_active"

    # Verify zero mutation in runtime state
    db_session.expire_all()
    assign_after_r1 = await db_session.get(TaskAssignment, assign_id)
    assert assign_after_r1.world_runtime_state == snapshot_before

    # Attempt 2: Ineligible subscription cancellation due to lock-in period
    r2 = await client.post(
        "/tools/cancel_subscription",
        json={"customer_id": "CUS-ADV-01", "subscription_id": "SUB-ADV-LOCKIN"},
        headers=headers,
    )
    assert r2.status_code == 200
    assert r2.json()["error"] == "INELIGIBLE"
    assert r2.json()["reason"] == "lock_in_period_active"

    # Verify zero mutation in runtime state
    db_session.expire_all()
    assign_after_r2 = await db_session.get(TaskAssignment, assign_id)
    assert assign_after_r2.world_runtime_state == snapshot_before


# 5. Partial Refund Accumulation and Boundary Limits
@pytest.mark.asyncio
async def test_partial_refund_accumulation_and_limits(
    client: AsyncClient, db_session: AsyncSession, setup_adversarial_env
):
    env = setup_adversarial_env
    headers = {"Authorization": f"Bearer {env['token']}"}
    assign_id = env["assignment_id"]

    # Step 1: Valid Partial Refund $40.00
    r1 = await client.post(
        "/tools/issue_refund",
        json={"transaction_id": "TXN-ADV-01", "amount": 40.0, "reason": "Partial 1"},
        headers=headers,
    )
    assert r1.status_code == 200
    assert r1.json()["status"] == "partially_refunded"
    assert r1.json()["transaction"]["refunded_amount"] == 40.0

    # Step 2: Attempt Over-refund $70.00 (40 + 70 = 110 > 100) -> Rejected
    r2 = await client.post(
        "/tools/issue_refund",
        json={"transaction_id": "TXN-ADV-01", "amount": 70.0, "reason": "Over-refund attempt"},
        headers=headers,
    )
    assert r2.status_code == 200
    assert r2.json()["error"] == "INELIGIBLE"
    assert r2.json()["reason"] == "amount_exceeds_transaction"

    # Verify DB refunded_amount is still 40.0
    db_session.expire_all()
    assign = await db_session.get(TaskAssignment, assign_id)
    tx = next(t for t in assign.world_runtime_state["transactions"] if t["id"] == "TXN-ADV-01")
    assert tx["refund_status"] == "partially_refunded"
    assert tx["refunded_amount"] == 40.0

    # Step 3: Valid Second Partial Refund $60.00 (40 + 60 = 100.0) -> Transitions to "refunded"
    r3 = await client.post(
        "/tools/issue_refund",
        json={"transaction_id": "TXN-ADV-01", "amount": 60.0, "reason": "Partial 2 final"},
        headers=headers,
    )
    assert r3.status_code == 200
    assert r3.json()["status"] == "refunded"
    assert r3.json()["transaction"]["refunded_amount"] == 100.0

    # Step 4: Further refund attempt $1.00 -> Rejected as already_refunded
    r4 = await client.post(
        "/tools/issue_refund",
        json={"transaction_id": "TXN-ADV-01", "amount": 1.0, "reason": "Extra refund"},
        headers=headers,
    )
    assert r4.status_code == 200
    assert r4.json()["error"] == "INELIGIBLE"
    assert r4.json()["reason"] in ("already_refunded", "amount_exceeds_transaction")


# 6. Concurrency Hardening: 5 Concurrent Requests Serialization
@pytest.mark.asyncio
async def test_concurrency_race_5_requests(client: AsyncClient, db_session: AsyncSession, setup_adversarial_env):
    env = setup_adversarial_env
    headers = {"Authorization": f"Bearer {env['token']}"}
    assign_id = env["assignment_id"]

    # Reset TXN-ADV-01 to fresh state
    assign = await db_session.get(TaskAssignment, assign_id)
    state = copy.deepcopy(assign.world_runtime_state)
    for t in state["transactions"]:
        if t["id"] == "TXN-ADV-01":
            t["refund_status"] = "none"
            t["refunded_amount"] = 0.0
    assign.world_runtime_state = state
    await db_session.commit()

    # Launch 5 concurrent full-refund attempts
    req = {"transaction_id": "TXN-ADV-01", "amount": 100.0, "reason": "Concurrent race 5"}
    resps5 = await asyncio.gather(*[client.post("/tools/issue_refund", json=req, headers=headers) for _ in range(5)])

    results5 = [r.json() for r in resps5]
    successes5 = [r for r in results5 if r.get("status") == "refunded"]
    rejections5 = [r for r in results5 if r.get("error") == "INELIGIBLE" and r.get("reason") == "already_refunded"]

    assert len(successes5) == 1
    assert len(rejections5) == 4

    # Verify state has exactly 100.0 refunded
    db_session.expire_all()
    assign = await db_session.get(TaskAssignment, assign_id)
    tx = next(t for t in assign.world_runtime_state["transactions"] if t["id"] == "TXN-ADV-01")
    assert tx["refund_status"] == "refunded"
    assert tx["refunded_amount"] == 100.0


# 7. Concurrency Budget Race: Exact Limit Enforcement
@pytest.mark.asyncio
async def test_concurrency_budget_race(client: AsyncClient, db_session: AsyncSession, setup_adversarial_env):
    env = setup_adversarial_env
    settings = env["settings_service"]
    # Set budget to 2 calls
    await settings.set("tool_call_budget_per_task", 2)
    await settings.set("rate_limit_tool_calls_per_min", 1000)

    headers = {"Authorization": f"Bearer {env['token']}"}

    # Fire 10 concurrent requests at the exact same millisecond
    tasks = [client.post("/tools/get_customer", json={"customer_id": "CUS-ADV-01"}, headers=headers) for _ in range(10)]
    resps = await asyncio.gather(*tasks)

    status_codes = [r.status_code for r in resps]
    ok_count = status_codes.count(200)
    throttled_count = status_codes.count(429)

    assert ok_count == 2
    assert throttled_count == 8

    for r in resps:
        if r.status_code == 429:
            assert r.json()["detail"]["error"] == "BUDGET_EXCEEDED"


# 8. Concurrency Rate Limit Race: Exact Limit Enforcement
@pytest.mark.asyncio
async def test_concurrency_rate_limit_race(client: AsyncClient, db_session: AsyncSession):
    settings_service = SettingsService(db_session)
    await settings_service.seed_defaults()
    await settings_service.set("rate_limit_tool_calls_per_min", 3)
    await settings_service.set("tool_call_budget_per_task", 1000)

    team, token = await register_team(db_session, "RateLimitRaceTeam")
    world = generate_world(seed=301)
    first_cust_id = world["customers"][0]["id"]
    task = Task(
        task_id="TASK-RL-RACE",
        dataset="dev",
        family="refund_request",
        variant="normal",
        input_payload={"customer_id": first_cust_id, "customer_message": "Hi"},
        world_state_seed=world,
        ground_truth={"expected_resolution": "refund", "must_escalate": False, "required_evidence": ["DOC-1001"]},
    )
    db_session.add(task)
    await db_session.commit()

    tool_service = ToolService(db_session, settings_service)
    await tool_service.assign_task(team.team_id, "TASK-RL-RACE")

    headers = {"Authorization": f"Bearer {token}"}

    # Fire 10 concurrent requests at the same millisecond
    tasks = [
        client.post("/tools/get_customer", json={"customer_id": first_cust_id}, headers=headers) for _ in range(10)
    ]
    resps = await asyncio.gather(*tasks)

    status_codes = [r.status_code for r in resps]
    ok_count = status_codes.count(200)
    throttled_count = status_codes.count(429)

    assert ok_count == 3
    assert throttled_count == 7

    for r in resps:
        if r.status_code == 429:
            assert r.json()["detail"]["error"] == "RATE_LIMIT_EXCEEDED"


# 9. Budget and Rate Limit Interaction Precedence
@pytest.mark.asyncio
async def test_budget_and_rate_limit_interaction(client: AsyncClient, db_session: AsyncSession, setup_adversarial_env):
    env = setup_adversarial_env
    settings = env["settings_service"]
    # Case: Budget is smaller than Rate Limit
    await settings.set("tool_call_budget_per_task", 2)
    await settings.set("rate_limit_tool_calls_per_min", 10)

    headers = {"Authorization": f"Bearer {env['token']}"}

    r1 = await client.post("/tools/get_customer", json={"customer_id": "CUS-ADV-01"}, headers=headers)
    assert r1.status_code == 200

    r2 = await client.post("/tools/get_customer", json={"customer_id": "CUS-ADV-01"}, headers=headers)
    assert r2.status_code == 200

    # 3rd request hits budget cap (BUDGET_EXCEEDED), NOT rate limit
    r3 = await client.post("/tools/get_customer", json={"customer_id": "CUS-ADV-01"}, headers=headers)
    assert r3.status_code == 429
    assert r3.json()["detail"]["error"] == "BUDGET_EXCEEDED"


# 10. Escalation Evidence Grounding Attacks
@pytest.mark.asyncio
async def test_escalation_evidence_grounding_adversarial(
    client: AsyncClient, db_session: AsyncSession, setup_adversarial_env
):
    env = setup_adversarial_env
    headers = {"Authorization": f"Bearer {env['token']}"}

    # Attack A: Fake doc ID never retrieved
    r_fake = await client.post(
        "/tools/escalate_case",
        json={"case_id": "TXN-ADV-01", "team": "billing_specialists", "reason": "Customer fraud under DOC-FAKE-9999"},
        headers=headers,
    )
    assert r_fake.status_code == 200
    assert r_fake.json()["error"] == "INVALID_ESCALATION"
    assert r_fake.json()["reason"] == "reason_not_grounded"

    # Attack B: Keyword-only reason without any evidence ID cited
    r_kw = await client.post(
        "/tools/escalate_case",
        json={
            "case_id": "TXN-ADV-01",
            "team": "billing_specialists",
            "reason": "Suspicious fraudulent activity and chargeback investigation required",
        },
        headers=headers,
    )
    assert r_kw.status_code == 200
    assert r_kw.json()["error"] == "INVALID_ESCALATION"
    assert r_kw.json()["reason"] == "reason_not_grounded"

    # Attack C: Valid DOC ID format ("DOC-1001"), but NOT yet retrieved in tool_call_logs for this task
    r_unretrieved = await client.post(
        "/tools/escalate_case",
        json={"case_id": "TXN-ADV-01", "team": "billing_specialists", "reason": "Policy violation per DOC-1001"},
        headers=headers,
    )
    assert r_unretrieved.status_code == 200
    assert r_unretrieved.json()["error"] == "INVALID_ESCALATION"
    assert r_unretrieved.json()["reason"] == "reason_not_grounded"

    # Legitimate Retrieval: Team calls get_document for DOC-1001
    r_doc = await client.post("/tools/get_document", json={"document_id": "DOC-1001"}, headers=headers)
    assert r_doc.status_code == 200

    # Legitimate Escalation: Now citations to DOC-1001 are accepted!
    r_valid = await client.post(
        "/tools/escalate_case",
        json={"case_id": "TXN-ADV-01", "team": "billing_specialists", "reason": "Policy violation per DOC-1001"},
        headers=headers,
    )
    assert r_valid.status_code == 200
    assert r_valid.json()["status"] == "escalated"


# 11. Input Validation Attacks (Pydantic / Schema Hardening)
@pytest.mark.asyncio
async def test_input_validation_attacks(client: AsyncClient, setup_adversarial_env):
    env = setup_adversarial_env
    headers = {"Authorization": f"Bearer {env['token']}"}

    # Attack A: Negative refund amount
    r_neg = await client.post(
        "/tools/issue_refund",
        json={"transaction_id": "TXN-ADV-01", "amount": -10.0, "reason": "Negative"},
        headers=headers,
    )
    assert r_neg.status_code == 422

    # Attack B: Zero refund amount
    r_zero = await client.post(
        "/tools/issue_refund", json={"transaction_id": "TXN-ADV-01", "amount": 0.0, "reason": "Zero"}, headers=headers
    )
    assert r_zero.status_code == 422

    # Attack C: Giant refund amount exceeding schema cap (> 1,000,000)
    r_giant = await client.post(
        "/tools/issue_refund",
        json={"transaction_id": "TXN-ADV-01", "amount": 1_000_001.0, "reason": "Giant"},
        headers=headers,
    )
    assert r_giant.status_code == 422

    # Attack D: Reversed date range (start_date > end_date)
    r_date = await client.post(
        "/tools/get_transactions",
        json={"customer_id": "CUS-ADV-01", "start_date": "2026-09-20T00:00:00Z", "end_date": "2026-09-10T00:00:00Z"},
        headers=headers,
    )
    assert r_date.status_code == 422
    assert r_date.json()["detail"]["error"] == "INVALID_DATE_RANGE"

    # Attack E: ID string length exceeding cap (> 100 chars)
    r_long_id = await client.post("/tools/get_customer", json={"customer_id": "C" * 105}, headers=headers)
    assert r_long_id.status_code == 422

    # Attack F: Whitespace string stripping
    r_strip = await client.post("/tools/get_customer", json={"customer_id": "  CUS-ADV-01  "}, headers=headers)
    assert r_strip.status_code == 200
    assert r_strip.json()["customer"]["id"] == "CUS-ADV-01"


# 12. Explicit Subscription Contract Distinction (PRD §4.1, §4.2)
@pytest.mark.asyncio
async def test_subscription_contract_distinction(client: AsyncClient, db_session: AsyncSession, setup_adversarial_env):
    """Proves the exact canonical distinction for subscriptions:
    1. Customer exists + NO subscription -> HTTP 200 {"subscription": null}
    2. Customer does NOT exist in world -> HTTP 404 CUSTOMER_NOT_FOUND
    3. Customer exists + active subscription -> HTTP 200 {"subscription": {...}}
    4. Cancel non-existent subscription -> HTTP 404 SUBSCRIPTION_NOT_FOUND
    5. Cancel subscription for non-existent customer -> HTTP 404 CUSTOMER_NOT_FOUND
    """
    env = setup_adversarial_env
    headers = {"Authorization": f"Bearer {env['token']}"}
    assign_id = env["assignment_id"]

    # Inject a customer who genuinely has NO subscription
    assign = await db_session.get(TaskAssignment, assign_id)
    runtime = copy.deepcopy(assign.world_runtime_state)
    runtime["customers"].append(
        {
            "id": "CUS-NO-SUB",
            "name": "No Sub Customer",
            "tier": "free",
            "region": "NA",
            "verification_status": "verified",
            "account_status": "active",
            "created_at": "2026-01-01T00:00:00Z",
        }
    )
    assign.world_runtime_state = runtime
    await db_session.commit()

    # Case 1: Customer exists, genuinely has NO subscription -> Must be HTTP 200 {"subscription": null}
    r1 = await client.post("/tools/get_subscription", json={"customer_id": "CUS-NO-SUB"}, headers=headers)
    assert r1.status_code == 200
    assert r1.json() == {"subscription": None}

    # Case 2: Customer does NOT exist in world -> Must be HTTP 404 CUSTOMER_NOT_FOUND
    r2 = await client.post("/tools/get_subscription", json={"customer_id": "CUS-DOES-NOT-EXIST"}, headers=headers)
    assert r2.status_code == 404
    assert r2.json()["detail"]["error"] == "CUSTOMER_NOT_FOUND"

    # Case 3: Customer exists, HAS subscription -> Must be HTTP 200 {"subscription": {...}}
    r3 = await client.post("/tools/get_subscription", json={"customer_id": "CUS-ADV-01"}, headers=headers)
    assert r3.status_code == 200
    assert r3.json()["subscription"] is not None
    assert r3.json()["subscription"]["id"] == "SUB-ADV-LOCKIN"

    # Case 4: Cancel non-existent subscription for existing customer -> Must be HTTP 404 SUBSCRIPTION_NOT_FOUND
    r4 = await client.post(
        "/tools/cancel_subscription",
        json={"customer_id": "CUS-ADV-01", "subscription_id": "SUB-PHANTOM-999"},
        headers=headers,
    )
    assert r4.status_code == 404
    assert r4.json()["detail"]["error"] == "SUBSCRIPTION_NOT_FOUND"

    # Case 5: Cancel subscription for non-existent customer -> Must be HTTP 404 CUSTOMER_NOT_FOUND
    r5 = await client.post(
        "/tools/cancel_subscription",
        json={"customer_id": "CUS-DOES-NOT-EXIST", "subscription_id": "SUB-ADV-LOCKIN"},
        headers=headers,
    )
    assert r5.status_code == 404
    assert r5.json()["detail"]["error"] == "CUSTOMER_NOT_FOUND"


# 12b. Immediate Multi-Worker Token Revocation (Zero Positive Auth Cache Delay)
@pytest.mark.asyncio
async def test_immediate_token_revocation_no_cache(
    client: AsyncClient, db_session: AsyncSession, setup_adversarial_env
):
    """Proves token regeneration revokes the old token IMMEDIATELY on the very next request
    with zero cache latency across all workers/processes.
    """
    from agent_arena.services.auth_service import regenerate_team_token

    env = setup_adversarial_env
    team = env["team"]
    old_token = env["token"]
    headers_old = {"Authorization": f"Bearer {old_token}"}

    # Request 1: Old token works
    r1 = await client.post("/tools/get_customer", json={"customer_id": "CUS-ADV-01"}, headers=headers_old)
    assert r1.status_code == 200

    # Admin/System regenerates token in database (simulating another worker)
    target_team_id = team.team_id
    db_session.expire_all()
    team_db = await db_session.get(Team, target_team_id)
    new_token = await regenerate_team_token(db_session, team_db)
    headers_new = {"Authorization": f"Bearer {new_token}"}

    # Request 2 with OLD token: MUST be rejected immediately (zero cache delay)
    r2 = await client.post("/tools/get_customer", json={"customer_id": "CUS-ADV-01"}, headers=headers_old)
    assert r2.status_code == 401
    assert "TOKEN_REVOKED" in r2.text or "UNAUTHORIZED" in r2.text

    # Request 3 with NEW token: MUST succeed immediately
    r3 = await client.post("/tools/get_customer", json={"customer_id": "CUS-ADV-01"}, headers=headers_new)
    assert r3.status_code == 200


# 12c. Immediate Team Suspension Across Workers (Zero Positive Auth Cache Delay)
@pytest.mark.asyncio
async def test_immediate_team_suspension_no_cache(client: AsyncClient, db_session: AsyncSession, setup_adversarial_env):
    """Proves team suspension/disqualification is enforced IMMEDIATELY on the very next request."""
    env = setup_adversarial_env
    team = env["team"]
    token = env["token"]
    headers = {"Authorization": f"Bearer {token}"}

    # Request 1: Active team works
    r1 = await client.post("/tools/get_customer", json={"customer_id": "CUS-ADV-01"}, headers=headers)
    assert r1.status_code == 200

    # Admin suspends team in database
    target_team_id = team.team_id
    db_session.expire_all()
    team_db = await db_session.get(Team, target_team_id)
    team_db.status = "suspended"
    await db_session.commit()

    # Request 2: MUST be forbidden immediately (403, zero cache delay)
    r2 = await client.post("/tools/get_customer", json={"customer_id": "CUS-ADV-01"}, headers=headers)
    assert r2.status_code == 403
    assert "suspended" in r2.text


# 12d. Exact Decimal / Numeric Arithmetic Precision
@pytest.mark.asyncio
async def test_exact_decimal_arithmetic_precision(client: AsyncClient, db_session: AsyncSession, setup_adversarial_env):
    """Proves currency operations use exact Decimal semantics without floating point drift."""
    env = setup_adversarial_env
    headers = {"Authorization": f"Bearer {env['token']}"}
    assign_id = env["assignment_id"]

    # Inject transaction with tricky fractional amounts ($100.00 total)
    assign = await db_session.get(TaskAssignment, assign_id)
    runtime = copy.deepcopy(assign.world_runtime_state)
    runtime["transactions"].append(
        {
            "id": "TXN-DECIMAL-EXACT",
            "customer_id": "CUS-ADV-01",
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
    assign.world_runtime_state = runtime
    await db_session.commit()

    # Step 1: Refund $19.99
    r1 = await client.post(
        "/tools/issue_refund",
        json={"transaction_id": "TXN-DECIMAL-EXACT", "amount": 19.99, "reason": "Exact decimal 1"},
        headers=headers,
    )
    assert r1.status_code == 200
    assert r1.json()["status"] == "partially_refunded"
    assert r1.json()["transaction"]["refunded_amount"] == 19.99

    # Step 2: Refund $80.01 (19.99 + 80.01 = 100.00 exactly)
    r2 = await client.post(
        "/tools/issue_refund",
        json={"transaction_id": "TXN-DECIMAL-EXACT", "amount": 80.01, "reason": "Exact decimal 2"},
        headers=headers,
    )
    assert r2.status_code == 200
    assert r2.json()["status"] == "refunded"
    assert r2.json()["transaction"]["refunded_amount"] == 100.00

    # Step 3: Refund $0.01 more -> Must be rejected (already_refunded / amount_exceeds_transaction)
    r3 = await client.post(
        "/tools/issue_refund",
        json={"transaction_id": "TXN-DECIMAL-EXACT", "amount": 0.01, "reason": "Over refund 1 cent"},
        headers=headers,
    )
    assert r3.status_code == 200
    assert r3.json()["error"] == "INELIGIBLE"


# 13. Failure Injection & Database Rollback
@pytest.mark.asyncio
async def test_database_failure_injection_rollback(
    client: AsyncClient, db_session: AsyncSession, setup_adversarial_env
):
    env = setup_adversarial_env
    headers = {"Authorization": f"Bearer {env['token']}"}
    assign_id = env["assignment_id"]

    # Snapshot before
    assign = await db_session.get(TaskAssignment, assign_id)
    snapshot = copy.deepcopy(assign.world_runtime_state)

    # Patch AsyncSession.commit to simulate database failure during commit
    with patch.object(AsyncSession, "commit", side_effect=RuntimeError("Simulated DB Disk Full / Connection Drop")):
        with pytest.raises(RuntimeError, match="Simulated DB Disk Full"):
            await client.post(
                "/tools/issue_refund",
                json={"transaction_id": "TXN-ADV-01", "amount": 50.0, "reason": "Rollback test"},
                headers=headers,
            )

    # Verify state rolled back cleanly
    db_session.expire_all()
    assign_after = await db_session.get(TaskAssignment, assign_id)
    assert assign_after.world_runtime_state == snapshot


# 14. Zero Token Leakage Scan
@pytest.mark.asyncio
async def test_zero_token_leakage_in_tool_logs(client: AsyncClient, db_session: AsyncSession):
    settings_service = SettingsService(db_session)
    await settings_service.seed_defaults()

    team, token = await register_team(db_session, "LeakScanTeam")
    world = generate_world(seed=401)
    first_cust_id = world["customers"][0]["id"]
    task = Task(
        task_id="TASK-LEAK-01",
        dataset="dev",
        family="refund_request",
        variant="normal",
        input_payload={"customer_id": first_cust_id, "customer_message": "Leak check"},
        world_state_seed=world,
        ground_truth={"expected_resolution": "refund", "must_escalate": False, "required_evidence": ["DOC-1001"]},
    )
    db_session.add(task)
    await db_session.commit()

    tool_service = ToolService(db_session, settings_service)
    await tool_service.assign_task(team.team_id, "TASK-LEAK-01")

    headers = {"Authorization": f"Bearer {token}"}

    # Make several tool calls with sensitive headers and payloads
    r_cust = await client.post("/tools/get_customer", json={"customer_id": first_cust_id}, headers=headers)
    assert r_cust.status_code == 200

    r_search = await client.post("/tools/search_knowledge", json={"query": "billing"}, headers=headers)
    assert r_search.status_code == 200

    # Scan all tool_call_logs in the database
    stmt = sa.select(ToolCallLog).where(ToolCallLog.team_id == team.team_id)
    result = await db_session.execute(stmt)
    logs = result.scalars().all()

    assert len(logs) >= 2
    for log in logs:
        # Verify request_payload does not contain the token
        req_str = str(log.request_payload)
        resp_str = str(log.response_payload)
        assert token not in req_str, f"Token leaked in request_payload: {req_str}"
        assert token not in resp_str, f"Token leaked in response_payload: {resp_str}"


# 15. Multi-Worker / Multi-Session Concurrency Serialization
@pytest.mark.asyncio
async def test_multi_session_row_locking_serialization(test_engine, db_session: AsyncSession):
    """Simulates 2 independent worker processes (separate database sessions) concurrently
    attempting to execute refund operations on the same task assignment.
    Proves that row-level locking strictly serializes workers with zero race conditions.
    """
    from sqlalchemy.ext.asyncio import async_sessionmaker

    settings_service = SettingsService(db_session)
    await settings_service.seed_defaults()

    team, token = await register_team(db_session, "MultiWorkerTeam")
    world = generate_world(seed=501)
    world["customers"].append(
        {
            "id": "CUS-MW-01",
            "name": "Worker User",
            "tier": "pro",
            "region": "NA",
            "verification_status": "verified",
            "account_status": "active",
            "created_at": "2026-01-01T00:00:00Z",
        }
    )
    world["transactions"].append(
        {
            "id": "TXN-MW-01",
            "customer_id": "CUS-MW-01",
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
        task_id="TASK-MW-01",
        dataset="dev",
        family="refund_request",
        variant="normal",
        input_payload={"customer_id": "CUS-MW-01", "customer_message": "Race"},
        world_state_seed=world,
        ground_truth={"expected_resolution": "refund", "must_escalate": False, "required_evidence": ["DOC-1001"]},
    )
    db_session.add(task)
    await db_session.commit()

    tool_service_init = ToolService(db_session, settings_service)
    assignment = await tool_service_init.assign_task(team.team_id, "TASK-MW-01")
    assign_id = assignment.id

    # Create 2 independent sessions simulating Worker 1 and Worker 2
    session_factory = async_sessionmaker(test_engine, expire_on_commit=False, class_=AsyncSession)

    async def worker_refund(worker_id: int):
        async with session_factory() as s:
            settings_s = SettingsService(s)
            ts = ToolService(s, settings_s)
            return await ts.run_tool(
                team=team,
                tool_name="issue_refund",
                payload={"transaction_id": "TXN-MW-01", "amount": 100.0, "reason": f"Worker {worker_id} refund"},
                is_action=True,
            )

    # Launch both workers concurrently
    res1, res2 = await asyncio.gather(worker_refund(1), worker_refund(2))

    results = [res1, res2]
    successes = [r for r in results if r.get("status") == "refunded"]
    rejections = [r for r in results if r.get("error") == "INELIGIBLE"]

    # Exactly one worker succeeded; exactly one was rejected with already_refunded
    assert len(successes) == 1
    assert len(rejections) == 1
    assert rejections[0]["reason"] == "already_refunded"

    # Verify final database state has exactly 100.0 refunded (not 200.0)
    db_session.expire_all()
    assign_final = await db_session.get(TaskAssignment, assign_id)
    tx = next(t for t in assign_final.world_runtime_state["transactions"] if t["id"] == "TXN-MW-01")
    assert tx["refund_status"] == "refunded"
    assert tx["refunded_amount"] == 100.0
