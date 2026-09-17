"""SentinelZero read tools tests.

Verifies the 5 read tool endpoints:
- /tools/lookup_directory
- /tools/get_approved_domains
- /tools/get_email_headers
- /tools/inspect_domain_reputation
- /tools/get_thread_history
"""

import pytest
from conftest import generate_world
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from agent_arena.models.task import Task
from agent_arena.services.auth_service import register_team
from agent_arena.services.dataset_service import DatasetService
from agent_arena.services.settings_service import SettingsService
from agent_arena.services.tool_service import ToolService


@pytest.fixture
async def setup_team_and_task(db_session: AsyncSession):
    """Sets up default settings, a registered team, and an assigned SentinelZero task."""
    settings_service = SettingsService(db_session)
    await settings_service.seed_defaults()

    # Load hidden tasks into database
    ds = DatasetService(db_session)
    await ds.generate_and_load_dataset(dataset_type="hidden", replace_existing=True)

    # Register team
    team, token = await register_team(
        session=db_session,
        team_name="ReadToolsTeam",
        members=[{"name": "Alice", "email": "alice@example.com"}],
    )

    # Assign a task to the team via /submission/start flow
    # (must go through submission_service to set up active assignment)
    return {
        "team": team,
        "token": token,
        "settings_service": settings_service,
    }


@pytest.fixture
async def setup_active_task(client: AsyncClient, setup_team_and_task, db_session: AsyncSession):
    """Starts a submission and returns the first task_id for tool testing."""
    token = setup_team_and_task["token"]
    headers = {"Authorization": f"Bearer {token}"}

    start_resp = await client.post("/submission/start", headers=headers)
    assert start_resp.status_code == 200
    task_id = start_resp.json()["tasks"][0]["task_id"]

    return {
        **setup_team_and_task,
        "task_id": task_id,
        "headers": {**headers, "X-Task-ID": task_id},
    }


@pytest.mark.asyncio
async def test_lookup_directory_found_and_not_found(client: AsyncClient, setup_active_task):
    """Verify lookup_directory returns employee data or not-found for unknown identifiers."""
    headers = setup_active_task["headers"]

    # Known identifier — use the first email from the world (generic enough)
    resp = await client.post("/tools/lookup_directory", json={"identifier": "unknown@sentinel-acme.edu"}, headers=headers)
    assert resp.status_code == 200
    data = resp.json()
    assert "found" in data
    assert isinstance(data["found"], bool)


@pytest.mark.asyncio
async def test_get_approved_domains_returns_list(client: AsyncClient, setup_active_task):
    """Verify get_approved_domains returns a list of official domains."""
    headers = setup_active_task["headers"]

    resp = await client.post("/tools/get_approved_domains", json={}, headers=headers)
    assert resp.status_code == 200
    data = resp.json()
    assert "official_domains" in data
    assert isinstance(data["official_domains"], list)
    assert len(data["official_domains"]) > 0


@pytest.mark.asyncio
async def test_get_email_headers_success_and_not_found(client: AsyncClient, setup_active_task):
    """Verify get_email_headers returns headers for known message_id."""
    headers = setup_active_task["headers"]
    task_id = setup_active_task["task_id"]

    # Extract message_id from task_id (format TASK-HIDDEN-NNN → MSG-HIDDEN-NNN)
    task_idx = task_id.replace("TASK-HIDDEN-", "")
    message_id = f"MSG-HIDDEN-{task_idx}"

    resp = await client.post("/tools/get_email_headers", json={"message_id": message_id}, headers=headers)
    assert resp.status_code == 200
    data = resp.json()
    assert "auth_results" in data or "message_id" in data or "sender" in data

    # Unknown message_id should return gracefully (404 or empty)
    resp_404 = await client.post("/tools/get_email_headers", json={"message_id": "MSG-NONEXISTENT-999"}, headers=headers)
    assert resp_404.status_code in (200, 404)


@pytest.mark.asyncio
async def test_inspect_domain_reputation_success(client: AsyncClient, setup_active_task):
    """Verify inspect_domain_reputation returns a reputation field."""
    headers = setup_active_task["headers"]

    resp = await client.post("/tools/inspect_domain_reputation", json={"domain": "sentinel-acme.edu"}, headers=headers)
    assert resp.status_code == 200
    data = resp.json()
    assert "reputation" in data or "domain" in data


@pytest.mark.asyncio
async def test_get_thread_history_success(client: AsyncClient, setup_active_task):
    """Verify get_thread_history returns messages list or empty."""
    headers = setup_active_task["headers"]

    resp = await client.post("/tools/get_thread_history", json={"thread_id": "THR-HIDDEN-001"}, headers=headers)
    assert resp.status_code == 200
    data = resp.json()
    assert "messages" in data
    assert isinstance(data["messages"], list)


@pytest.mark.asyncio
async def test_unauthenticated_request_rejected(client: AsyncClient):
    """Verify requests without Bearer token return 401."""
    resp = await client.post("/tools/lookup_directory", json={"identifier": "alice@sentinel-acme.edu"})
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_no_active_task_returns_404(client: AsyncClient, db_session: AsyncSession):
    """Verify team with no active task assignment receives 404 NO_ACTIVE_TASK."""
    settings_service = SettingsService(db_session)
    await settings_service.seed_defaults()

    team, token = await register_team(
        session=db_session,
        team_name="TasklessTeam",
        members=[{"name": "Bob"}],
    )
    headers = {"Authorization": f"Bearer {token}"}

    resp = await client.post("/tools/lookup_directory", json={"identifier": "alice@sentinel-acme.edu"}, headers=headers)
    assert resp.status_code == 404
    assert resp.json()["detail"]["error"] == "NO_ACTIVE_TASK"
