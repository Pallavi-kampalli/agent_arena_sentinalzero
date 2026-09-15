from collections.abc import AsyncGenerator
from typing import Annotated
from fastapi import Depends, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from agent_arena.db import get_session_maker
from agent_arena.models.team import Team
from agent_arena.services.settings_service import SettingsService


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
