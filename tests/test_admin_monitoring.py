import uuid
from datetime import UTC, datetime

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from agent_arena.config import get_config
from agent_arena.models.task import Task
from agent_arena.models.team import Team
from agent_arena.models.tool_call_log import ToolCallLog
from agent_arena.services.auth_service import hash_token
from agent_arena.services.settings_service import SettingsService
from conftest import generate_world


@pytest.fixture
def admin_headers():
    secret = get_config().ADMIN_PANEL_SECRET
    return {"X-Admin-Secret": secret}


@pytest.mark.asyncio
async def test_admin_tool_logs_filters(client: AsyncClient, admin_headers, db_session: AsyncSession):
    """Admin can query and filter execution logs by team, task, tool, and rejections."""
    team_a = Team(team_id=uuid.uuid4(), team_name="LogTeamA", bearer_token_hash=hash_token("la"), status="active")
    team_b = Team(team_id=uuid.uuid4(), team_name="LogTeamB", bearer_token_hash=hash_token("lb"), status="active")
    db_session.add_all([team_a, team_b])

    # Add 4 tool logs:
    # 1. Team A, task 1, search_orders, normal
    # 2. Team A, task 1, refund_order, was_enforcement_rejection=True
    # 3. Team A, task 2, get_customer, normal
    # 4. Team B, task 3, get_customer, normal
    log1 = ToolCallLog(
        team_id=team_a.team_id,
        task_id="TASK-LOG-001",
        tool_name="search_orders",
        was_enforcement_rejection=False,
        latency_ms=12.5,
        created_at=datetime.now(UTC),
    )
    log2 = ToolCallLog(
        team_id=team_a.team_id,
        task_id="TASK-LOG-001",
        tool_name="refund_order",
        was_enforcement_rejection=True,
        latency_ms=8.0,
        created_at=datetime.now(UTC),
    )
    log3 = ToolCallLog(
        team_id=team_a.team_id,
        task_id="TASK-LOG-002",
        tool_name="get_customer",
        was_enforcement_rejection=False,
        latency_ms=15.0,
        created_at=datetime.now(UTC),
    )
    log4 = ToolCallLog(
        team_id=team_b.team_id,
        task_id="TASK-LOG-003",
        tool_name="get_customer",
        was_enforcement_rejection=False,
        latency_ms=14.0,
        created_at=datetime.now(UTC),
    )
    db_session.add_all([log1, log2, log3, log4])
    await db_session.commit()

    # 1. Filter by team_id
    resp_team = await client.get(f"/admin/tool-logs?team_id={team_a.team_id}", headers=admin_headers)
    assert resp_team.status_code == 200
    assert resp_team.json()["total"] == 3

    # 2. Filter by rejections_only
    resp_rej = await client.get("/admin/tool-logs?rejections_only=true", headers=admin_headers)
    assert resp_rej.status_code == 200
    assert resp_rej.json()["total"] == 1
    assert resp_rej.json()["logs"][0]["tool_name"] == "refund_order"
    assert resp_rej.json()["logs"][0]["was_enforcement_rejection"] is True

    # 3. Filter by task_id and tool_name
    resp_task = await client.get("/admin/tool-logs?task_id=TASK-LOG-001&tool_name=search_orders", headers=admin_headers)
    assert resp_task.status_code == 200
    assert resp_task.json()["total"] == 1


@pytest.mark.asyncio
async def test_admin_system_health_and_pool_warning(client: AsyncClient, admin_headers, db_session: AsyncSession):
    """Admin health check returns DB status, active metrics, and warns on low task pool."""
    settings = SettingsService(db_session)
    await settings.seed_defaults()

    # When 0 tasks in DB, hidden_tasks < hidden_task_count (200) -> task_pool_warning must be True
    resp1 = await client.get("/admin/health", headers=admin_headers)
    assert resp1.status_code == 200
    h1 = resp1.json()
    assert h1["database"] == "healthy"
    assert h1["status"] == "healthy"
    assert h1["task_pool_warning"] is True

    # Set required hidden_task_count to 5 and seed 6 tasks -> task_pool_warning must become False
    await settings.set("hidden_task_count", 5)
    for i in range(6):
        t = Task(
            task_id=f"TASK-HLTH-{i:03d}",
            dataset="hidden",
            family="refund_request",
            variant="normal",
            input_payload={"customer_id": f"C-{i}"},
            world_state_seed=generate_world(seed=8000 + i),
            ground_truth={"expected_resolution": "refund"},
        )
        db_session.add(t)
    await db_session.commit()

    resp2 = await client.get("/admin/health", headers=admin_headers)
    assert resp2.status_code == 200
    h2 = resp2.json()
    assert h2["hidden_tasks_available"] >= 6
    assert h2["task_pool_warning"] is False
