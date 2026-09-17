import asyncio

import pytest
from conftest import generate_world
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from agent_arena.models.task import Task
from agent_arena.models.task_assignment import TaskAssignment
from agent_arena.services.auth_service import register_team
from agent_arena.services.settings_service import SettingsService
from agent_arena.services.tool_service import ToolService


@pytest.mark.asyncio
async def test_cross_team_isolation(client: AsyncClient, db_session: AsyncSession):
    """Verify Team A and Team B are completely isolated even with identical entity ID strings."""
    settings_service = SettingsService(db_session)
    await settings_service.seed_defaults()

    # 1. Register Team A and Team B
    team_a, token_a = await register_team(db_session, "TeamAlpha")
    team_b, token_b = await register_team(db_session, "TeamBeta")

    world_a = generate_world(seed=42)
    world_b = generate_world(seed=42)

    task_a = Task(
        task_id="TASK-ISO-A",
        dataset="hidden",
        input_payload={"message_id": "MSG-SHARED-99", "sender": "alice@sentinel-acme.edu"},
        world_state_seed=world_a,
        ground_truth={"expected_resolution": "quarantine"},
    )
    task_b = Task(
        task_id="TASK-ISO-B",
        dataset="hidden",
        input_payload={"message_id": "MSG-SHARED-99", "sender": "alice@sentinel-acme.edu"},
        world_state_seed=world_b,
        ground_truth={"expected_resolution": "quarantine"},
    )

    db_session.add(task_a)
    db_session.add(task_b)
    await db_session.commit()

    tool_service = ToolService(db_session, settings_service)
    await tool_service.assign_task(team_a.team_id, "TASK-ISO-A")
    assign_b = await tool_service.assign_task(team_b.team_id, "TASK-ISO-B")

    headers_a = {"Authorization": f"Bearer {token_a}", "X-Task-ID": "TASK-ISO-A"}
    headers_b = {"Authorization": f"Bearer {token_b}", "X-Task-ID": "TASK-ISO-B"}

    # 3. Team A reads directory
    resp_a_cust = await client.post("/tools/lookup_directory", json={"identifier": "alice@sentinel-acme.edu"}, headers=headers_a)
    assert resp_a_cust.status_code == 200

    # Team B reads directory
    resp_b_cust = await client.post("/tools/lookup_directory", json={"identifier": "alice@sentinel-acme.edu"}, headers=headers_b)
    assert resp_b_cust.status_code == 200

    # 4. Team A quarantines MSG-SHARED-99
    resp_a_ref = await client.post(
        "/tools/quarantine_message",
        json={"message_id": "MSG-SHARED-99", "reason": "Team A quarantine"},
        headers=headers_a,
    )
    assert resp_a_ref.status_code == 200
    assert resp_a_ref.json()["status"] == "quarantined"

    # 5. Verify Team B's world state remains UNMUTATED by Team A's action
    assign_b_id = assign_b.id
    db_session.expire_all()
    assign_b_db = await db_session.get(TaskAssignment, assign_b_id)
    assert assign_b_db.world_runtime_state.get("delivery_status") != "quarantined"

    # Team B can still process its own MSG-SHARED-99
    resp_b_ref = await client.post(
        "/tools/quarantine_message",
        json={"message_id": "MSG-SHARED-99", "reason": "Team B quarantine"},
        headers=headers_b,
    )
    assert resp_b_ref.status_code == 200
    assert resp_b_ref.json()["status"] == "quarantined"


@pytest.mark.asyncio
async def test_concurrent_quarantine_attempts(client: AsyncClient, db_session: AsyncSession):
    """Verify concurrent quarantine requests for the same message serialize and complete idempotently."""
    settings_service = SettingsService(db_session)
    await settings_service.seed_defaults()

    team, token = await register_team(db_session, "RaceTeam")
    world = generate_world(seed=42)

    task = Task(
        task_id="TASK-RACE-01",
        dataset="hidden",
        input_payload={"message_id": "MSG-RACE-01", "sender": "attacker@evil.example"},
        world_state_seed=world,
        ground_truth={"expected_resolution": "quarantine"},
    )
    db_session.add(task)
    await db_session.commit()

    tool_service = ToolService(db_session, settings_service)
    assignment = await tool_service.assign_task(team.team_id, "TASK-RACE-01")

    headers = {"Authorization": f"Bearer {token}", "X-Task-ID": "TASK-RACE-01"}
    req_body = {"message_id": "MSG-RACE-01", "reason": "Concurrent race test"}

    # Issue 2 concurrent requests
    resps = await asyncio.gather(
        client.post("/tools/quarantine_message", json=req_body, headers=headers),
        client.post("/tools/quarantine_message", json=req_body, headers=headers),
    )

    statuses = [r.status_code for r in resps]
    assert all(s == 200 for s in statuses)

    results = [r.json() for r in resps]
    successes = [r for r in results if r.get("status") == "quarantined"]
    assert len(successes) >= 1

    # Verify final database state has delivery_status == quarantined
    assignment_id = assignment.id
    db_session.expire_all()
    assign_db = await db_session.get(TaskAssignment, assignment_id)
    assert assign_db.world_runtime_state.get("delivery_status") == "quarantined"


@pytest.mark.asyncio
async def test_dynamic_rate_limiter_setting(client: AsyncClient, db_session: AsyncSession):
    """Verify changing rate_limit_tool_calls_per_min in settings dynamically throttles without code change."""
    settings_service = SettingsService(db_session)
    await settings_service.seed_defaults()

    team, token = await register_team(db_session, "ThrottleTeam")
    world = generate_world(seed=42)
    task = Task(
        task_id="TASK-THROTTLE-01",
        dataset="hidden",
        input_payload={"message_id": "MSG-01", "sender": "alice@sentinel-acme.edu"},
        world_state_seed=world,
        ground_truth={"expected_resolution": "allow"},
    )
    db_session.add(task)
    await db_session.commit()

    tool_service = ToolService(db_session, settings_service)
    await tool_service.assign_task(team.team_id, "TASK-THROTTLE-01")

    # Set rate limit to 2 calls per minute
    await settings_service.set("rate_limit_tool_calls_per_min", 2)

    headers = {"Authorization": f"Bearer {token}", "X-Task-ID": "TASK-THROTTLE-01"}

    # Call 1: Success
    r1 = await client.post("/tools/lookup_directory", json={"identifier": "alice@sentinel-acme.edu"}, headers=headers)
    assert r1.status_code == 200

    # Call 2: Success
    r2 = await client.post("/tools/lookup_directory", json={"identifier": "alice@sentinel-acme.edu"}, headers=headers)
    assert r2.status_code == 200

    # Call 3: Throttled (429 Rate Limit Exceeded)
    r3 = await client.post("/tools/lookup_directory", json={"identifier": "alice@sentinel-acme.edu"}, headers=headers)
    assert r3.status_code == 429
    assert r3.json()["detail"]["error"] == "RATE_LIMIT_EXCEEDED"

    # Now dynamically increase limit to 10 in settings
    await settings_service.set("rate_limit_tool_calls_per_min", 10)

    # Call 4: Now succeeds immediately without redeployment!
    r4 = await client.post("/tools/lookup_directory", json={"identifier": "alice@sentinel-acme.edu"}, headers=headers)
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
        dataset="hidden",
        input_payload={"message_id": "MSG-01", "sender": "alice@sentinel-acme.edu"},
        world_state_seed=world,
        ground_truth={"expected_resolution": "allow"},
    )
    db_session.add(task)
    await db_session.commit()

    tool_service = ToolService(db_session, settings_service)
    await tool_service.assign_task(team.team_id, "TASK-BUDGET-01")

    # Set budget to 3 calls for this task
    await settings_service.set("tool_call_budget_per_task", 3)
    # Ensure rate limit doesn't interfere
    await settings_service.set("rate_limit_tool_calls_per_min", 100)

    headers = {"Authorization": f"Bearer {token}", "X-Task-ID": "TASK-BUDGET-01"}

    # Calls 1, 2, 3 succeed
    for _ in range(3):
        res = await client.post("/tools/lookup_directory", json={"identifier": "alice@sentinel-acme.edu"}, headers=headers)
        assert res.status_code == 200

    # Call 4: Exceeds budget
    r4 = await client.post("/tools/lookup_directory", json={"identifier": "alice@sentinel-acme.edu"}, headers=headers)
    assert r4.status_code == 429
    assert r4.json()["detail"]["error"] == "BUDGET_EXCEEDED"
