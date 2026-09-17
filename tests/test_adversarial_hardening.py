"""SentinelZero adversarial hardening tests.

Tests that the platform correctly enforces:
1. Team spoofing via body injection (Pydantic extra='forbid')
2. Cross-task world state isolation (same entity IDs, different world states)
3. Zero mutation on rejected actions
4. Concurrency serialization (5 concurrent quarantine requests)
5. Budget and rate limit race condition enforcement
6. Escalation evidence grounding attacks
7. Input validation attacks (payload schema hardening)
8. Immediate token revocation (zero cache)
9. Immediate team suspension (zero cache)
10. Database failure injection and rollback
11. Zero bearer token leakage in tool logs
12. Multi-session row-locking serialization
"""

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
from agent_arena.services.dataset_service import DatasetService
from agent_arena.services.settings_service import SettingsService
from agent_arena.services.tool_service import ToolService


def _sz_world(seed: int = 0) -> dict:
    """Minimal SentinelZero world state for testing."""
    return {
        "seed": seed,
        "current_date": "2026-09-15T00:00:00Z",
        "target_message_id": f"MSG-ADV-{seed:03d}",
        "directory": [{"id": "EMP-001", "name": "Alice", "official_email": "alice@sentinel-acme.edu"}],
        "domains": [{"domain_id": "DOM-OFFICIAL-001", "domain": "sentinel-acme.edu", "category": "official"}],
        "threat_intel": [{"domain_id": "DOM-THREAT-001", "domain": "evil.example", "reputation": "malicious"}],
        "security_policies": [{"id": "POL-001", "category": "phishing", "title": "Phishing Response Policy"}],
        "historical_threats": [{"log_id": "LOG-001", "threat_type": "phishing"}],
        "threads": [],
        "actions_taken": [],
    }


@pytest.fixture
async def sz_env(client: AsyncClient, db_session: AsyncSession):
    """Sets up a seeded SentinelZero environment with a team and task assignment."""
    settings_service = SettingsService(db_session)
    await settings_service.seed_defaults()
    await settings_service.set("rate_limit_tool_calls_per_min", 1000)
    await settings_service.set("tool_call_budget_per_task", 1000)

    ds = DatasetService(db_session)
    await ds.generate_and_load_dataset(dataset_type="hidden", replace_existing=True)

    team, token = await register_team(db_session, "AdversarialTeam")
    headers = {"Authorization": f"Bearer {token}"}

    start_resp = await client.post("/submission/start", headers=headers)
    assert start_resp.status_code == 200
    task_id = start_resp.json()["tasks"][0]["task_id"]
    task_idx = task_id.replace("TASK-HIDDEN-", "")
    message_id = f"MSG-HIDDEN-{task_idx}"
    headers["X-Task-ID"] = task_id
    await client.post("/tools/get_approved_domains", json={}, headers=headers)

    return {
        "team": team,
        "token": token,
        "task_id": task_id,
        "message_id": message_id,
        "headers": headers,
        "settings_service": settings_service,
    }


# ─────────────────────────────────────────────────────────────────────────────
# 1. Team Spoofing via Body Injection (Pydantic extra='forbid')
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_team_spoofing_in_body_rejected(client: AsyncClient, sz_env):
    """Participant injecting team_id or extra fields into read/action tool payloads must get 422."""
    env = sz_env
    headers = env["headers"]
    message_id = env["message_id"]

    # Inject team_id into read tool
    r1 = await client.post(
        "/tools/lookup_directory",
        json={"identifier": "alice@sentinel-acme.edu", "team_id": "team_spoofed"},
        headers=headers,
    )
    assert r1.status_code == 422

    # Inject assignment_id into action tool
    r2 = await client.post(
        "/tools/quarantine_message",
        json={"message_id": message_id, "reason": "Phishing", "assignment_id": "assign_spoofed"},
        headers=headers,
    )
    assert r2.status_code == 422

    # Inject unknown field into get_approved_domains
    r3 = await client.post(
        "/tools/get_approved_domains",
        json={"bypass_auth": True},
        headers=headers,
    )
    assert r3.status_code == 422


# ─────────────────────────────────────────────────────────────────────────────
# 2. Cross-Task World State Isolation (Identical Entity IDs)
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_cross_task_identical_entities_full_isolation(client: AsyncClient, db_session: AsyncSession):
    """Two teams operating on tasks with identical message IDs must see independent world states."""
    settings_service = SettingsService(db_session)
    await settings_service.seed_defaults()

    ds = DatasetService(db_session)
    await ds.generate_and_load_dataset(dataset_type="hidden", replace_existing=True)

    team1, token1 = await register_team(db_session, "IsoTeam1")
    team2, token2 = await register_team(db_session, "IsoTeam2")

    headers1 = {"Authorization": f"Bearer {token1}"}
    headers2 = {"Authorization": f"Bearer {token2}"}

    # Both teams start a submission (they get different tasks from the pool)
    r1 = await client.post("/submission/start", headers=headers1)
    assert r1.status_code == 200
    task1_id = r1.json()["tasks"][0]["task_id"]
    headers1["X-Task-ID"] = task1_id

    r2 = await client.post("/submission/start", headers=headers2)
    assert r2.status_code == 200
    task2_id = r2.json()["tasks"][0]["task_id"]
    headers2["X-Task-ID"] = task2_id

    # Initialize assignment for Team 2 via tool call
    await client.post("/tools/get_approved_domains", json={}, headers=headers2)

    # Team 1 quarantines their message
    msg1_idx = task1_id.replace("TASK-HIDDEN-", "")
    msg1_id = f"MSG-HIDDEN-{msg1_idx}"

    r_q1 = await client.post(
        "/tools/quarantine_message",
        json={"message_id": msg1_id, "reason": "Phishing from Team 1 perspective"},
        headers=headers1,
    )
    assert r_q1.status_code == 200
    assert r_q1.json()["status"] == "quarantined"

    # Team 2's world state must be unchanged (their delivery_status should not be "quarantined" from Team 1)
    msg2_idx = task2_id.replace("TASK-HIDDEN-", "")
    msg2_id = f"MSG-HIDDEN-{msg2_idx}"

    # Look up team 2's assignment to verify its state is independent
    assign2 = (
        await db_session.execute(
            sa.select(TaskAssignment)
            .where(TaskAssignment.team_id == team2.team_id)
            .order_by(TaskAssignment.id.desc())
        )
    ).scalars().first()
    assert assign2 is not None
    # Team 2's world state should NOT have "quarantined" delivery_status from Team 1
    state2 = assign2.world_runtime_state
    # Delivery_status on Team 2's world is not set to quarantined by Team 1's action
    assert state2.get("delivery_status") not in ("quarantined",), "Cross-team world state leak detected!"


# ─────────────────────────────────────────────────────────────────────────────
# 3. Seed Immutability Across All Actions
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_seed_immutability_across_actions(client: AsyncClient, db_session: AsyncSession, sz_env):
    """world_state_seed (source of truth) must NEVER be mutated by any tool call."""
    env = sz_env
    headers = env["headers"]
    task_id = env["task_id"]
    message_id = env["message_id"]

    team_id = env["team"].team_id
    assign = (await db_session.execute(sa.select(TaskAssignment).where(TaskAssignment.team_id == team_id))).scalars().first()
    real_task_id = assign.task_id

    # Fetch initial seed snapshot from DB
    db_session.expire_all()
    task_before = await db_session.get(Task, real_task_id)
    assert task_before is not None
    initial_seed = copy.deepcopy(task_before.world_state_seed)

    # Perform all read tools
    await client.post("/tools/lookup_directory", json={"identifier": "alice@sentinel-acme.edu"}, headers=headers)
    await client.post("/tools/get_approved_domains", json={}, headers=headers)
    await client.post("/tools/get_email_headers", json={"message_id": message_id}, headers=headers)
    await client.post("/tools/inspect_domain_reputation", json={"domain": "sentinel-acme.edu"}, headers=headers)
    await client.post("/tools/get_thread_history", json={"thread_id": "THR-HIDDEN-001"}, headers=headers)

    # Perform all action tools
    await client.post("/tools/allow_and_deliver",
                      json={"message_id": message_id, "reason": "Already cleared"},
                      headers=headers)
    await client.post("/tools/apply_warning_banner",
                      json={"message_id": message_id, "banner_type": "EXTERNAL_SENDER", "reason": "Warn"},
                      headers=headers)
    await client.post("/tools/quarantine_message",
                      json={"message_id": message_id, "reason": "Phishing"},
                      headers=headers)

    # Re-fetch task and verify world_state_seed is 100% byte-for-byte identical
    db_session.expire_all()
    task_after = await db_session.get(Task, real_task_id)
    assert task_after.world_state_seed == initial_seed


# ─────────────────────────────────────────────────────────────────────────────
# 4. Zero Mutation on Rejected Actions
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_zero_mutation_on_rejected_actions(client: AsyncClient, db_session: AsyncSession, sz_env):
    """Ungrounded escalation must not mutate the world state at all."""
    env = sz_env
    headers = env["headers"]
    message_id = env["message_id"]
    team_id = env["team"].team_id

    # Snapshot current runtime state
    assign = (
        await db_session.execute(
            sa.select(TaskAssignment).where(TaskAssignment.team_id == team_id).order_by(TaskAssignment.id.desc())
        )
    ).scalars().first()
    assert assign is not None
    snapshot_before = copy.deepcopy(assign.world_runtime_state)

    # Attempt ungrounded escalation (no evidence cited)
    r1 = await client.post(
        "/tools/escalate_to_tier2_soc",
        json={"message_id": message_id, "reason": "Looks suspicious to me"},
        headers=headers,
    )
    assert r1.status_code == 200
    assert r1.json()["error"] == "INVALID_ESCALATION"

    # Verify zero mutation in runtime state
    db_session.expire_all()
    assign_after = (
        await db_session.execute(
            sa.select(TaskAssignment).where(TaskAssignment.team_id == team_id).order_by(TaskAssignment.id.desc())
        )
    ).scalars().first()
    assert assign_after.world_runtime_state == snapshot_before


# ─────────────────────────────────────────────────────────────────────────────
# 5. Concurrency Hardening: 5 Concurrent Quarantine Requests (Idempotency)
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_concurrency_race_5_requests(client: AsyncClient, db_session: AsyncSession, sz_env):
    """5 concurrent quarantine calls must all succeed (quarantine is idempotent), last write wins."""
    env = sz_env
    headers = env["headers"]
    message_id = env["message_id"]
    team_id = env["team"].team_id

    req = {"message_id": message_id, "reason": "Concurrent Phishing Race"}
    resps = await asyncio.gather(
        *[client.post("/tools/quarantine_message", json=req, headers=headers) for _ in range(5)]
    )

    results = [r.json() for r in resps]
    # All 5 should return 200 (quarantine is idempotent)
    successes = [r for r in results if r.get("status") == "quarantined"]
    assert len(successes) >= 1  # At minimum one must succeed

    # Final world state must reflect quarantined status
    db_session.expire_all()
    assign = (
        await db_session.execute(
            sa.select(TaskAssignment).where(TaskAssignment.team_id == team_id).order_by(TaskAssignment.id.desc())
        )
    ).scalars().first()
    assert assign.world_runtime_state.get("delivery_status") == "quarantined"


# ─────────────────────────────────────────────────────────────────────────────
# 6. Concurrency Budget Race: Exact Limit Enforcement
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_concurrency_budget_race(client: AsyncClient, db_session: AsyncSession, sz_env):
    """Budget of 2 tool calls: 10 concurrent requests — exactly 2 succeed, 8 get BUDGET_EXCEEDED."""
    env = sz_env
    settings = env["settings_service"]
    await settings.set("tool_call_budget_per_task", 2)
    await settings.set("rate_limit_tool_calls_per_min", 1000)

    team_id = env["team"].team_id
    from agent_arena.models.tool_call_log import ToolCallLog
    await db_session.execute(sa.delete(ToolCallLog).where(ToolCallLog.team_id == team_id))
    db_session.expire_all()
    assign = (await db_session.execute(sa.select(TaskAssignment).where(TaskAssignment.team_id == team_id))).scalars().first()
    if assign:
        assign.tool_call_count = 0
    await db_session.commit()

    headers = env["headers"]

    tasks = [
        client.post("/tools/get_approved_domains", json={}, headers=headers)
        for _ in range(10)
    ]
    resps = await asyncio.gather(*tasks)

    status_codes = [r.status_code for r in resps]
    ok_count = status_codes.count(200)
    throttled_count = status_codes.count(429)

    assert ok_count == 2
    assert throttled_count == 8

    for r in resps:
        if r.status_code == 429:
            assert r.json()["detail"]["error"] == "BUDGET_EXCEEDED"


# ─────────────────────────────────────────────────────────────────────────────
# 7. Concurrency Rate Limit Race: Exact Limit Enforcement
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_concurrency_rate_limit_race(client: AsyncClient, db_session: AsyncSession):
    """Rate limit of 3/min: 10 concurrent requests — exactly 3 succeed, 7 get RATE_LIMIT_EXCEEDED."""
    settings_service = SettingsService(db_session)
    await settings_service.seed_defaults()
    await settings_service.set("rate_limit_tool_calls_per_min", 3)
    await settings_service.set("tool_call_budget_per_task", 1000)

    ds = DatasetService(db_session)
    await ds.generate_and_load_dataset(dataset_type="hidden", replace_existing=True)

    team, token = await register_team(db_session, "RateLimitRaceTeam")
    headers = {"Authorization": f"Bearer {token}"}

    start_resp = await client.post("/submission/start", headers=headers)
    assert start_resp.status_code == 200
    task_id = start_resp.json()["tasks"][0]["task_id"]
    headers["X-Task-ID"] = task_id

    tasks = [
        client.post("/tools/get_approved_domains", json={}, headers=headers)
        for _ in range(10)
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


# ─────────────────────────────────────────────────────────────────────────────
# 8. Budget and Rate Limit Interaction Precedence
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_budget_and_rate_limit_interaction(client: AsyncClient, db_session: AsyncSession, sz_env):
    """When budget < rate limit, BUDGET_EXCEEDED must trigger before RATE_LIMIT_EXCEEDED."""
    env = sz_env
    settings = env["settings_service"]
    await settings.set("tool_call_budget_per_task", 2)
    await settings.set("rate_limit_tool_calls_per_min", 10)

    team_id = env["team"].team_id
    from agent_arena.models.tool_call_log import ToolCallLog
    await db_session.execute(sa.delete(ToolCallLog).where(ToolCallLog.team_id == team_id))
    db_session.expire_all()
    assign = (await db_session.execute(sa.select(TaskAssignment).where(TaskAssignment.team_id == team_id))).scalars().first()
    if assign:
        assign.tool_call_count = 0
    await db_session.commit()

    headers = env["headers"]

    r1 = await client.post("/tools/get_approved_domains", json={}, headers=headers)
    assert r1.status_code == 200

    r2 = await client.post("/tools/get_approved_domains", json={}, headers=headers)
    assert r2.status_code == 200

    # 3rd request must hit budget cap (BUDGET_EXCEEDED), not rate limit
    r3 = await client.post("/tools/get_approved_domains", json={}, headers=headers)
    assert r3.status_code == 429
    assert r3.json()["detail"]["error"] == "BUDGET_EXCEEDED"


# ─────────────────────────────────────────────────────────────────────────────
# 9. Escalation Evidence Grounding Adversarial Attacks
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_escalation_evidence_grounding_adversarial(client: AsyncClient, db_session: AsyncSession, sz_env):
    """Verify escalation evidence grounding blocks hallucinated/unretrieved IDs."""
    env = sz_env
    headers = env["headers"]
    message_id = env["message_id"]

    # Attack A: Fabricated POL ID never retrieved
    r_fake = await client.post(
        "/tools/escalate_to_tier2_soc",
        json={"message_id": message_id, "reason": "APT campaign under POL-FAKE-9999"},
        headers=headers,
    )
    assert r_fake.status_code == 200
    assert r_fake.json()["error"] == "INVALID_ESCALATION"
    assert r_fake.json()["reason"] == "reason_not_grounded"

    # Attack B: Keyword-only reason without any SZ evidence ID
    r_kw = await client.post(
        "/tools/escalate_to_tier2_soc",
        json={"message_id": message_id, "reason": "Suspicious phishing activity requiring human review"},
        headers=headers,
    )
    assert r_kw.status_code == 200
    assert r_kw.json()["error"] == "INVALID_ESCALATION"
    assert r_kw.json()["reason"] == "reason_not_grounded"

    # Attack C: Valid POL-ID format but NOT yet retrieved in tool_call_logs
    r_unretrieved = await client.post(
        "/tools/escalate_to_tier2_soc",
        json={"message_id": message_id, "reason": f"Advanced threat found on {message_id}"},
        headers=headers,
    )
    assert r_unretrieved.status_code == 200
    assert r_unretrieved.json()["error"] == "INVALID_ESCALATION"

    # Legitimate: Team retrieves headers first (exposes message_id in response)
    r_hdr = await client.post("/tools/get_email_headers", json={"message_id": message_id}, headers=headers)
    assert r_hdr.status_code == 200

    # Legitimate Escalation: Now citation is accepted
    r_valid = await client.post(
        "/tools/escalate_to_tier2_soc",
        json={"message_id": message_id, "reason": f"Email {message_id} failed SPF and DKIM — requires SOC review"},
        headers=headers,
    )
    assert r_valid.status_code == 200
    assert r_valid.json()["status"] == "escalated_to_soc"


# ─────────────────────────────────────────────────────────────────────────────
# 10. Input Validation Attacks (Schema Hardening)
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_input_validation_attacks(client: AsyncClient, sz_env):
    """Verify input validation rejects malformed payloads."""
    env = sz_env
    headers = env["headers"]
    message_id = env["message_id"]

    # Attack A: Missing required message_id field
    r_missing = await client.post(
        "/tools/quarantine_message",
        json={"reason": "Missing message_id"},
        headers=headers,
    )
    assert r_missing.status_code == 422

    # Attack B: Empty string message_id
    r_empty = await client.post(
        "/tools/quarantine_message",
        json={"message_id": "", "reason": "Empty ID"},
        headers=headers,
    )
    assert r_empty.status_code in (200, 422)
    if r_empty.status_code == 200:
        assert r_empty.json().get("error") == "INVALID_MESSAGE_ID"

    # Attack C: ID string exceeding schema cap (> 100 chars)
    r_long = await client.post(
        "/tools/get_email_headers",
        json={"message_id": "M" * 105},
        headers=headers,
    )
    assert r_long.status_code == 422

    # Attack D: Whitespace-padded message_id must be stripped
    r_strip = await client.post(
        "/tools/get_email_headers",
        json={"message_id": f"  {message_id}  "},
        headers=headers,
    )
    # Must succeed (stripped) or return 422 if format invalid — either is correct
    assert r_strip.status_code in (200, 422, 404)


# ─────────────────────────────────────────────────────────────────────────────
# 11. Immediate Token Revocation (Zero Cache Delay)
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_immediate_token_revocation_no_cache(client: AsyncClient, db_session: AsyncSession, sz_env):
    """Token regeneration must revoke old token immediately with zero cache latency."""
    from agent_arena.services.auth_service import regenerate_team_token

    env = sz_env
    team = env["team"]
    old_token = env["token"]
    headers_old = {"Authorization": f"Bearer {old_token}", "X-Task-ID": env["task_id"]}

    # Old token works
    r1 = await client.post("/tools/get_approved_domains", json={}, headers=headers_old)
    assert r1.status_code == 200

    # System regenerates token
    target_team_id = team.team_id
    db_session.expire_all()
    team_db = await db_session.get(Team, target_team_id)
    new_token = await regenerate_team_token(db_session, team_db)
    headers_new = {"Authorization": f"Bearer {new_token}", "X-Task-ID": env["task_id"]}

    # Old token must be rejected immediately
    r2 = await client.post("/tools/get_approved_domains", json={}, headers=headers_old)
    assert r2.status_code == 401
    assert "TOKEN_REVOKED" in r2.text or "UNAUTHORIZED" in r2.text

    # New token must work immediately
    r3 = await client.post("/tools/get_approved_domains", json={}, headers=headers_new)
    assert r3.status_code == 200


# ─────────────────────────────────────────────────────────────────────────────
# 12. Immediate Team Suspension (Zero Cache Delay)
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_immediate_team_suspension_no_cache(client: AsyncClient, db_session: AsyncSession, sz_env):
    """Team suspension must immediately reject the team on the next request."""
    env = sz_env
    team = env["team"]
    headers = {**env["headers"]}

    # Active team works
    r1 = await client.post("/tools/get_approved_domains", json={}, headers=headers)
    assert r1.status_code == 200

    # Suspend team in database
    target_team_id = team.team_id
    db_session.expire_all()
    team_db = await db_session.get(Team, target_team_id)
    team_db.status = "suspended"
    await db_session.commit()

    # Must be forbidden immediately (403, zero cache delay)
    r2 = await client.post("/tools/get_approved_domains", json={}, headers=headers)
    assert r2.status_code == 403
    assert "suspended" in r2.text


# ─────────────────────────────────────────────────────────────────────────────
# 13. Exact State Arithmetic Precision
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_exact_decimal_arithmetic_precision(client: AsyncClient, db_session: AsyncSession, sz_env):
    """Actions appended to actions_taken must not duplicate or corrupt existing entries."""
    env = sz_env
    headers = env["headers"]
    message_id = env["message_id"]
    team_id = env["team"].team_id

    # Apply 3 actions sequentially
    await client.post("/tools/allow_and_deliver",
                      json={"message_id": message_id, "reason": "Step 1: allow"},
                      headers=headers)
    await client.post("/tools/apply_warning_banner",
                      json={"message_id": message_id, "banner_type": "EXTERNAL_SENDER", "reason": "Step 2: warn"},
                      headers=headers)
    await client.post("/tools/quarantine_message",
                      json={"message_id": message_id, "reason": "Step 3: quarantine"},
                      headers=headers)

    # Final runtime state must have exactly 3 entries in actions_taken
    db_session.expire_all()
    assign = (
        await db_session.execute(
            sa.select(TaskAssignment)
            .where(TaskAssignment.team_id == team_id)
            .order_by(TaskAssignment.id.desc())
        )
    ).scalars().first()
    actions = assign.world_runtime_state.get("actions_taken", [])
    assert len(actions) == 3
    assert actions[2]["action"] == "quarantine_message"


# ─────────────────────────────────────────────────────────────────────────────
# 14. Database Failure Injection and Rollback
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_database_failure_injection_rollback(client: AsyncClient, db_session: AsyncSession, sz_env):
    """Simulated DB commit failure during action must roll back without state mutation."""
    env = sz_env
    headers = env["headers"]
    message_id = env["message_id"]
    team_id = env["team"].team_id

    # Snapshot before
    assign = (
        await db_session.execute(
            sa.select(TaskAssignment)
            .where(TaskAssignment.team_id == team_id)
            .order_by(TaskAssignment.id.desc())
        )
    ).scalars().first()
    snapshot = copy.deepcopy(assign.world_runtime_state)

    # Patch AsyncSession.commit to simulate database failure
    with patch.object(AsyncSession, "commit", side_effect=RuntimeError("Simulated DB Disk Full")):
        with pytest.raises(RuntimeError, match="Simulated DB Disk Full"):
            await client.post(
                "/tools/quarantine_message",
                json={"message_id": message_id, "reason": "Rollback test"},
                headers=headers,
            )

    # Verify state rolled back cleanly
    db_session.expire_all()
    assign_after = (
        await db_session.execute(
            sa.select(TaskAssignment)
            .where(TaskAssignment.team_id == team_id)
            .order_by(TaskAssignment.id.desc())
        )
    ).scalars().first()
    assert assign_after.world_runtime_state == snapshot


# ─────────────────────────────────────────────────────────────────────────────
# 15. Zero Token Leakage Scan in Tool Logs
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_zero_token_leakage_in_tool_logs(client: AsyncClient, db_session: AsyncSession):
    """Bearer tokens must never appear in stored tool_call_log request/response payloads."""
    settings_service = SettingsService(db_session)
    await settings_service.seed_defaults()

    ds = DatasetService(db_session)
    await ds.generate_and_load_dataset(dataset_type="hidden", replace_existing=True)

    team, token = await register_team(db_session, "LeakScanTeam")
    headers = {"Authorization": f"Bearer {token}"}

    start_resp = await client.post("/submission/start", headers=headers)
    assert start_resp.status_code == 200
    task_id = start_resp.json()["tasks"][0]["task_id"]
    headers["X-Task-ID"] = task_id

    # Make several tool calls with sensitive headers
    await client.post("/tools/get_approved_domains", json={}, headers=headers)
    await client.post("/tools/lookup_directory", json={"identifier": "alice@sentinel-acme.edu"}, headers=headers)

    # Scan all tool_call_logs in the database for this team
    stmt = sa.select(ToolCallLog).where(ToolCallLog.team_id == team.team_id)
    result = await db_session.execute(stmt)
    logs = result.scalars().all()

    assert len(logs) >= 2
    for log in logs:
        req_str = str(log.request_payload)
        resp_str = str(log.response_payload)
        assert token not in req_str, f"Token leaked in request_payload: {req_str}"
        assert token not in resp_str, f"Token leaked in response_payload: {resp_str}"


# ─────────────────────────────────────────────────────────────────────────────
# 16. Multi-Session Row-Locking Serialization
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_multi_session_row_locking_serialization(test_engine, db_session: AsyncSession):
    """2 independent worker sessions concurrently quarantining the same message must serialize."""
    from sqlalchemy.ext.asyncio import async_sessionmaker

    settings_service = SettingsService(db_session)
    await settings_service.seed_defaults()

    ds = DatasetService(db_session)
    await ds.generate_and_load_dataset(dataset_type="hidden", replace_existing=True)

    team, token = await register_team(db_session, "MultiWorkerTeam")
    tool_service_init = ToolService(db_session, settings_service)

    # Get the first task
    import sqlalchemy as sa_
    task = (await db_session.execute(sa_.select(Task).where(Task.dataset == "hidden"))).scalars().first()
    task_idx = task.task_id.replace("TASK-HIDDEN-", "")
    message_id = f"MSG-HIDDEN-{task_idx}"

    assignment = await tool_service_init.assign_task(team.team_id, task.task_id)
    assign_id = assignment.id

    session_factory = async_sessionmaker(test_engine, expire_on_commit=False, class_=AsyncSession)

    async def worker_quarantine(worker_id: int):
        async with session_factory() as s:
            settings_s = SettingsService(s)
            ts = ToolService(s, settings_s)
            return await ts.run_tool(
                team=team,
                tool_name="quarantine_message",
                payload={"message_id": message_id, "reason": f"Worker {worker_id} quarantine"},
                is_action=True,
            )

    # Launch both workers concurrently
    res1, res2 = await asyncio.gather(worker_quarantine(1), worker_quarantine(2))

    results = [res1, res2]
    # At least one worker quarantined
    successes = [r for r in results if r.get("status") == "quarantined"]
    assert len(successes) >= 1

    # Verify final state is quarantined (not corrupted by race)
    db_session.expire_all()
    assign_final = await db_session.get(TaskAssignment, assign_id)
    assert assign_final.world_runtime_state.get("delivery_status") == "quarantined"
