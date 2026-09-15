import hashlib
import time
import uuid
from typing import Any, Optional
import jwt
from sqlalchemy.ext.asyncio import AsyncSession
import sqlalchemy as sa

from agent_arena.config import get_config
from agent_arena.models.team import Team


def hash_token(token: str) -> str:
    """Computes deterministic SHA-256 hash of a bearer token string."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def create_bearer_token(
    team_id: uuid.UUID,
    token_version: int,
    expiry_hours: Optional[int] = None,
) -> str:
    """Issues a cryptographically signed JWT bearer token for a team."""
    config = get_config()
    now = int(time.time())
    payload: dict[str, Any] = {
        "sub": str(team_id),
        "version": token_version,
        "iat": now,
    }
    if expiry_hours is not None:
        payload["exp"] = now + int(expiry_hours * 3600)

    token = jwt.encode(payload, config.JWT_SIGNING_SECRET, algorithm="HS256")
    return token


def decode_bearer_token(token: str) -> dict[str, Any]:
    """Decodes and verifies a bearer token's signature and expiration."""
    config = get_config()
    payload = jwt.decode(token, config.JWT_SIGNING_SECRET, algorithms=["HS256"], options={"verify_exp": True})
    return payload


async def register_team(
    session: AsyncSession,
    team_name: str,
    members: Optional[Any] = None,
    github_repo_url: Optional[str] = None,
    expiry_hours: Optional[int] = None,
) -> tuple[Team, str]:
    """Registers a new team, stores the hashed token, and returns the team and plaintext token."""
    team_id = uuid.uuid4()
    token_version = 1
    raw_token = create_bearer_token(team_id, token_version, expiry_hours=expiry_hours)
    token_hash = hash_token(raw_token)

    team = Team(
        team_id=team_id,
        team_name=team_name,
        members=members,
        github_repo_url=github_repo_url,
        bearer_token_hash=token_hash,
        token_version=token_version,
        status="active",
    )
    session.add(team)
    await session.commit()
    await session.refresh(team)
    return team, raw_token


async def regenerate_team_token(
    session: AsyncSession,
    team: Team,
    expiry_hours: Optional[int] = None,
) -> str:
    """Bumps token_version, revoking previous tokens immediately, and issues a new token."""
    team.token_version += 1
    raw_token = create_bearer_token(team.team_id, team.token_version, expiry_hours=expiry_hours)
    team.bearer_token_hash = hash_token(raw_token)
    await session.commit()
    await session.refresh(team)
    return raw_token


async def authenticate_bearer_token(session: AsyncSession, raw_token: str) -> Team:
    """Validates token signature, token_version, hash, and team status.
    
    Raises ValueError with specific message on failure.
    """
    try:
        payload = decode_bearer_token(raw_token)
    except jwt.ExpiredSignatureError:
        raise ValueError("TOKEN_EXPIRED")
    except jwt.InvalidTokenError:
        raise ValueError("INVALID_TOKEN")

    team_id_str = payload.get("sub")
    claimed_version = payload.get("version")
    if not team_id_str or claimed_version is None:
        raise ValueError("MALFORMED_TOKEN_PAYLOAD")

    try:
        team_id = uuid.UUID(team_id_str)
    except (ValueError, TypeError):
        raise ValueError("INVALID_TEAM_ID_IN_TOKEN")

    # Load team from database
    result = await session.execute(sa.select(Team).where(Team.team_id == team_id))
    team = result.scalar_one_or_none()

    if team is None:
        raise ValueError("TEAM_NOT_FOUND")

    # Status check
    if team.status != "active":
        raise ValueError(f"TEAM_STATUS_{team.status.upper()}")

    # Version check (revocation check)
    if team.token_version != claimed_version:
        raise ValueError("TOKEN_REVOKED")

    # Hash verification
    if hash_token(raw_token) != team.bearer_token_hash:
        raise ValueError("TOKEN_HASH_MISMATCH")

    return team
