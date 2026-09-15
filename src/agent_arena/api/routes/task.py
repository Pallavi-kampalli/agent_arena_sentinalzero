from typing import Annotated, Any

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from agent_arena.api.deps import get_current_team, get_db_session, get_settings_service
from agent_arena.models.team import Team
from agent_arena.schemas.submission import (
    TaskStartResponse,
    TaskSubmitRequest,
    TaskSubmitResponse,
)
from agent_arena.services.settings_service import SettingsService
from agent_arena.services.submission_service import SubmissionService

router = APIRouter(prefix="/task", tags=["task"])


@router.post("/start", response_model=TaskStartResponse)
async def start_task(
    team: Annotated[Team, Depends(get_current_team)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
    settings_service: Annotated[SettingsService, Depends(get_settings_service)],
) -> TaskStartResponse:
    """Assigns the next unassigned task for the team's current active submission."""
    service = SubmissionService(session, settings_service)
    res = await service.start_next_task(team.team_id)
    return TaskStartResponse(**res)


@router.post("/submit", response_model=TaskSubmitResponse)
async def submit_task(
    req: TaskSubmitRequest,
    team: Annotated[Team, Depends(get_current_team)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
    settings_service: Annotated[SettingsService, Depends(get_settings_service)],
) -> TaskSubmitResponse:
    """Submits agent decision and evidence for the currently active task."""
    service = SubmissionService(session, settings_service)
    res = await service.submit_task(team.team_id, req)
    return TaskSubmitResponse(**res)
