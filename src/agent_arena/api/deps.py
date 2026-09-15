import hmac
import re
from collections.abc import AsyncGenerator
from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from agent_arena.config import get_config
from agent_arena.db import get_session_maker
from agent_arena.models.team import Team
from agent_arena.services.settings_service import SettingsService


@dataclass(frozen=True)
class AdminUser:
    username: str = "admin"
    role: str = "admin"


async def get_db_session() -> AsyncGenerator[AsyncSession, None]:
    session_maker = get_session_maker()
    async with session_maker() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise


async def get_settings_service(
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> SettingsService:
    return SettingsService(session)


async def get_current_team(request: Request) -> Team:
    team = getattr(request.state, "team", None)
    if not team:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required",
        )
    return team


async def get_current_admin(request: Request) -> AdminUser:
    """Authenticates admin via X-Admin-Secret header or Authorization: Bearer <secret>.

    Uses constant-time comparison (hmac.compare_digest) against config.ADMIN_PANEL_SECRET.
    Rejects missing, empty, or invalid credentials (including participant JWTs) with HTTP 401.
    """
    config = get_config()
    secret = request.headers.get("X-Admin-Secret")
    if not secret:
        auth = request.headers.get("Authorization")
        if auth and auth.startswith("Bearer "):
            secret = auth.removeprefix("Bearer ").strip()

    if not secret:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={
                "error": "ADMIN_UNAUTHORIZED",
                "message": "Missing admin secret header (X-Admin-Secret or Authorization: Bearer <secret>).",
            },
        )

    # Constant-time comparison to prevent timing attacks
    if not hmac.compare_digest(secret, config.ADMIN_PANEL_SECRET):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"error": "ADMIN_UNAUTHORIZED", "message": "Invalid admin secret."},
        )

    raw_actor = request.headers.get("X-Admin-Actor")
    actor = "admin"
    if raw_actor:
        cleaned = re.sub(r"[^a-zA-Z0-9_\-]", "", raw_actor).strip()
        if cleaned:
            actor = cleaned[:50]

    return AdminUser(username=actor, role="admin")
