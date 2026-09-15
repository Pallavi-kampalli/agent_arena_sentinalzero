import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from agent_arena.config import get_config
from agent_arena.services.auth_service import create_bearer_token


@pytest.mark.asyncio
async def test_admin_auth_missing_credentials(client: AsyncClient):
    """Admin endpoints must return 401 when no credentials are provided."""
    resp_get = await client.get("/admin/settings")
    assert resp_get.status_code == 401
    assert resp_get.json()["detail"]["error"] == "ADMIN_UNAUTHORIZED"

    resp_post = await client.post("/admin/teams", json={"team_name": "GhostTeam"})
    assert resp_post.status_code == 401
    assert resp_post.json()["detail"]["error"] == "ADMIN_UNAUTHORIZED"


@pytest.mark.asyncio
async def test_admin_auth_invalid_credentials(client: AsyncClient):
    """Admin endpoints must reject invalid secrets via both header types."""
    resp = await client.get("/admin/settings", headers={"X-Admin-Secret": "invalid-secret-key-1234567890"})
    assert resp.status_code == 401
    assert resp.json()["detail"]["error"] == "ADMIN_UNAUTHORIZED"

    resp = await client.get("/admin/settings", headers={"Authorization": "Bearer invalid-secret-key-1234567890"})
    assert resp.status_code == 401
    assert resp.json()["detail"]["error"] == "ADMIN_UNAUTHORIZED"


@pytest.mark.asyncio
async def test_admin_auth_rejects_participant_jwt(client: AsyncClient, db_session: AsyncSession):
    """A participant JWT token must never grant admin privileges."""
    team_id = uuid.uuid4()
    participant_token = create_bearer_token(team_id=team_id, token_version=1)

    resp = await client.get("/admin/settings", headers={"Authorization": f"Bearer {participant_token}"})
    assert resp.status_code == 401
    assert resp.json()["detail"]["error"] == "ADMIN_UNAUTHORIZED"

    resp = await client.get("/admin/settings", headers={"X-Admin-Secret": participant_token})
    assert resp.status_code == 401
    assert resp.json()["detail"]["error"] == "ADMIN_UNAUTHORIZED"


@pytest.mark.asyncio
async def test_admin_auth_valid_x_admin_secret_header(client: AsyncClient, db_session: AsyncSession):
    """Valid X-Admin-Secret header authenticates the admin."""
    secret = get_config().ADMIN_PANEL_SECRET
    resp = await client.get("/admin/settings", headers={"X-Admin-Secret": secret})
    assert resp.status_code == 200
    assert "settings" in resp.json()


@pytest.mark.asyncio
async def test_admin_auth_valid_bearer_token(client: AsyncClient, db_session: AsyncSession):
    """Valid Authorization: Bearer <secret> authenticates the admin."""
    secret = get_config().ADMIN_PANEL_SECRET
    resp = await client.get("/admin/settings", headers={"Authorization": f"Bearer {secret}"})
    assert resp.status_code == 200
    assert "settings" in resp.json()


@pytest.mark.asyncio
async def test_admin_dashboard_html_endpoint(client: AsyncClient):
    """GET /admin/dashboard returns valid HTML without requiring participant token."""
    resp = await client.get("/admin/dashboard")
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]
    assert "Agent Arena — Admin Control Plane" in resp.text


@pytest.mark.asyncio
async def test_admin_actor_header_is_metadata_only(client: AsyncClient, db_session: AsyncSession):
    """X-Admin-Actor is strictly metadata and cannot bypass authentication or elevate privileges."""
    # Attempting to access admin routes with X-Admin-Actor: super_admin but no secret must fail
    resp_no_secret = await client.get("/admin/settings", headers={"X-Admin-Actor": "super_admin"})
    assert resp_no_secret.status_code == 401
    assert resp_no_secret.json()["detail"]["error"] == "ADMIN_UNAUTHORIZED"

    # With valid secret, X-Admin-Actor is recorded for audit attribution
    secret = get_config().ADMIN_PANEL_SECRET
    resp_valid = await client.put(
        "/admin/settings/time_budget_per_task_seconds",
        json={"value": 150},
        headers={"X-Admin-Secret": secret, "X-Admin-Actor": "lead_auditor"},
    )
    assert resp_valid.status_code == 200
    assert resp_valid.json()["changed_by"] == "lead_auditor"
