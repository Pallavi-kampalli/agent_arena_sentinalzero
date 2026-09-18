import hashlib
import secrets
import string
import time
import uuid
from typing import Any

import jwt
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from agent_arena.config import get_config
from agent_arena.models.team import RevokedToken, Team

# In-memory cache of revoked token hashes for instant cross-worker/in-process checks
_REVOKED_HASHES_CACHE: set[str] = set()


def hash_token(token: str) -> str:
    """Computes deterministic SHA-256 hash of a bearer token string."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def generate_short_token(length: int = 8) -> str:
    """Generates a secure, participant-friendly random alphanumeric bearer token."""
    chars = string.ascii_letters + string.digits
    return "".join(secrets.choice(chars) for _ in range(length))


def create_bearer_token(
    team_id: uuid.UUID | None = None,
    token_version: int = 1,
    expiry_hours: int | None = None,
    length: int = 8,
) -> str:
    """Issues a bearer token for a team.

    By default, generates a participant-friendly ~8 character alphanumeric token.
    If expiry_hours is explicitly set, generates a cryptographically signed JWT with expiration claims.
    """
    if expiry_hours is not None and team_id is not None:
        config = get_config()
        now = int(time.time())
        payload: dict[str, Any] = {
            "sub": str(team_id),
            "version": token_version,
            "iat": now,
            "exp": now + int(expiry_hours * 3600),
        }
        return jwt.encode(payload, config.JWT_SIGNING_SECRET, algorithm="HS256")

    return generate_short_token(length=length)


def decode_bearer_token(token: str) -> dict[str, Any]:
    """Decodes and verifies a bearer token's signature and expiration."""
    config = get_config()
    payload = jwt.decode(token, config.JWT_SIGNING_SECRET, algorithms=["HS256"], options={"verify_exp": True})
    return payload


async def register_team(
    session: AsyncSession,
    team_name: str,
    members: Any | None = None,
    github_repo_url: str | None = None,
    expiry_hours: int | None = None,
) -> tuple[Team, str]:
    """Registers a new team, stores the hashed token, and returns the team and plaintext token."""
    team_id = uuid.uuid4()
    token_version = 1

    # Ensure unique token hash across teams
    while True:
        raw_token = create_bearer_token(team_id, token_version, expiry_hours=expiry_hours)
        token_hash = hash_token(raw_token)
        existing = await session.execute(sa.select(Team.team_id).where(Team.bearer_token_hash == token_hash))
        if existing.scalar_one_or_none() is None:
            break

    # Assign sequential 5-digit display_id starting at 10001
    max_disp_res = await session.execute(sa.select(sa.func.max(Team.display_id)))
    max_disp = max_disp_res.scalar()
    next_disp = 10001 if max_disp is None else max_disp + 1
    team_code = f"T-{next_disp}"

    team = Team(
        team_id=team_id,
        team_name=team_name,
        display_id=next_disp,
        team_code=team_code,
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
    expiry_hours: int | None = None,
) -> str:
    """Bumps token_version, revoking previous tokens immediately, and issues a new token."""
    old_hash = team.bearer_token_hash
    _REVOKED_HASHES_CACHE.add(old_hash)

    # Persist revoked token in revoked_tokens table
    revoked = RevokedToken(
        token_hash=old_hash,
        team_id=team.team_id,
    )
    session.add(revoked)

    team.token_version += 1
    while True:
        raw_token = create_bearer_token(team.team_id, team.token_version, expiry_hours=expiry_hours)
        token_hash = hash_token(raw_token)
        existing = await session.execute(sa.select(Team.team_id).where(Team.bearer_token_hash == token_hash))
        if existing.scalar_one_or_none() is None:
            break

    team.bearer_token_hash = token_hash
    await session.commit()
    await session.refresh(team)
    return raw_token


async def authenticate_bearer_token(session: AsyncSession, raw_token: str) -> Team:
    """Validates token signature/hash, token_version, and team status.

    Always validates directly against the database to ensure immediate token revocation
    and immediate team suspension enforcement across all workers/processes (PRD §4, §11).
    Raises ValueError with specific message on failure.
    """
    token_h = hash_token(raw_token)

    # If the token is formatted as a JWT, verify expiration and signature integrity first
    if raw_token.count(".") == 2:
        try:
            decode_bearer_token(raw_token)
        except jwt.ExpiredSignatureError:
            raise ValueError("TOKEN_EXPIRED")
        except jwt.InvalidTokenError:
            raise ValueError("INVALID_TOKEN")

    # 1. Primary path: look up active team by current bearer_token_hash (O(1) indexed lookup)
    result = await session.execute(sa.select(Team).where(Team.bearer_token_hash == token_h))
    team = result.scalar_one_or_none()

    if team is not None:
        if team.status != "active":
            raise ValueError(f"TEAM_STATUS_{team.status.upper()}")
        return team

    # 2. Revocation check: verify if token hash was previously revoked on token regeneration
    if token_h in _REVOKED_HASHES_CACHE:
        raise ValueError("TOKEN_REVOKED")

    try:
        revoked = await session.execute(sa.select(RevokedToken.id).where(RevokedToken.token_hash == token_h))
        if revoked.scalar_one_or_none() is not None:
            _REVOKED_HASHES_CACHE.add(token_h)
            raise ValueError("TOKEN_REVOKED")
    except sa.exc.ProgrammingError:
        pass

    # 3. Fallback path for JWT tokens (backward compatibility for explicit exp/JWT tokens)
    if raw_token.count(".") == 2:
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

        result = await session.execute(sa.select(Team).where(Team.team_id == team_id))
        team = result.scalar_one_or_none()

        if team is None:
            raise ValueError("TEAM_NOT_FOUND")

        if team.status != "active":
            raise ValueError(f"TEAM_STATUS_{team.status.upper()}")

        if team.token_version != claimed_version:
            raise ValueError("TOKEN_REVOKED")

        if token_h != team.bearer_token_hash:
            raise ValueError("TOKEN_HASH_MISMATCH")

        return team

    raise ValueError("INVALID_TOKEN")
