import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from agent_arena.models.team import Team
from agent_arena.services.auth_service import (
    create_bearer_token,
    hash_token,
    regenerate_team_token,
    register_team,
)


@pytest.mark.asyncio
async def test_health_check_unauthenticated(client: AsyncClient):
    """Verify /health is accessible without bearer authentication."""
    response = await client.get("/health")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "ok"
    assert "phase" in data
    assert "environment" in data


@pytest.mark.asyncio
async def test_protected_route_missing_auth(client: AsyncClient):
    """Verify protected routes return 401 when Authorization header is missing."""
    response = await client.get("/team/me")
    assert response.status_code == 401
    assert response.json()["error"] == "UNAUTHORIZED"


@pytest.mark.asyncio
async def test_protected_route_invalid_token(client: AsyncClient):
    """Verify protected routes return 401 when token is invalid."""
    response = await client.get(
        "/team/me",
        headers={"Authorization": "Bearer invalid_garbage_token_123"},
    )
    assert response.status_code == 401
    assert response.json()["error"] == "UNAUTHORIZED"


@pytest.mark.asyncio
async def test_protected_route_success(client: AsyncClient, db_session: AsyncSession):
    """Verify active team can access protected routes with valid bearer token."""
    team, token = await register_team(db_session, team_name="Team Cyber")

    response = await client.get(
        "/team/me",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 200
    data = response.json()
    assert data["team_id"] == str(team.team_id)
    assert data["team_name"] == "Team Cyber"
    assert data["status"] == "active"
    assert data["token_version"] == 1


@pytest.mark.asyncio
async def test_suspended_or_disqualified_team(client: AsyncClient, db_session: AsyncSession):
    """Verify suspended or disqualified teams receive 403 Forbidden."""
    team, token = await register_team(db_session, team_name="Team Rogue")
    team.status = "suspended"
    await db_session.commit()

    response = await client.get(
        "/team/me",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 403
    assert response.json()["error"] == "FORBIDDEN"
    assert "suspended" in response.json()["detail"]


@pytest.mark.asyncio
async def test_token_regeneration_revokes_old_token(client: AsyncClient, db_session: AsyncSession):
    """Verify bumping token_version instantly invalidates old token and accepts new token."""
    team, old_token = await register_team(db_session, team_name="Team Phoenix")

    # Old token works
    res_ok = await client.get("/team/me", headers={"Authorization": f"Bearer {old_token}"})
    assert res_ok.status_code == 200

    # Regenerate token
    new_token = await regenerate_team_token(db_session, team)

    # Old token is rejected (401)
    res_old = await client.get("/team/me", headers={"Authorization": f"Bearer {old_token}"})
    assert res_old.status_code == 401
    assert res_old.json()["error"] == "UNAUTHORIZED"

    # New token works (200)
    res_new = await client.get("/team/me", headers={"Authorization": f"Bearer {new_token}"})
    assert res_new.status_code == 200
    assert res_new.json()["token_version"] == 2


@pytest.mark.asyncio
async def test_expired_token(client: AsyncClient, db_session: AsyncSession):
    """Verify expired token returns 401 with TOKEN_EXPIRED."""
    team_id = uuid.uuid4()
    # Create token expired in the past: expiry_hours = -1
    token = create_bearer_token(team_id, token_version=1, expiry_hours=-1)
    token_hash = hash_token(token)

    team = Team(
        team_id=team_id,
        team_name="Expired Team",
        bearer_token_hash=token_hash,
        token_version=1,
        status="active",
    )
    db_session.add(team)
    await db_session.commit()

    response = await client.get("/team/me", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 401
    assert response.json()["error"] == "TOKEN_EXPIRED"


@pytest.mark.asyncio
async def test_short_8_char_bearer_token_lifecycle(client: AsyncClient, db_session: AsyncSession):
    """Verify newly registered teams receive clean 8-character bearer tokens that authenticate and revoke."""
    team, token = await register_team(db_session, team_name="ShortTokenTeam")
    assert len(token) == 8
    assert token.isalnum()

    # 1. Valid 8-char token authenticates
    res_ok = await client.get("/team/me", headers={"Authorization": f"Bearer {token}"})
    assert res_ok.status_code == 200
    assert res_ok.json()["team_name"] == "ShortTokenTeam"

    # 2. Token regeneration issues another 8-char token and invalidates the previous one
    new_token = await regenerate_team_token(db_session, team)
    assert len(new_token) == 8
    assert new_token != token

    # Old 8-char token fails immediately
    res_old = await client.get("/team/me", headers={"Authorization": f"Bearer {token}"})
    assert res_old.status_code == 401

    # New 8-char token works
    res_new = await client.get("/team/me", headers={"Authorization": f"Bearer {new_token}"})
    assert res_new.status_code == 200

