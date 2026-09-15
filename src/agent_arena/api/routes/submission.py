from typing import Annotated, Any
import uuid

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from agent_arena.api.deps import get_current_team, get_db_session, get_settings_service
from agent_arena.models.team import Team
from agent_arena.schemas.submission import (
    SubmissionFinalizeResponse,
    SubmissionStartResponse,
    SubmissionStatusResponse,
)
from agent_arena.services.settings_service import SettingsService
from agent_arena.services.submission_service import SubmissionService

router = APIRouter(prefix="/submission", tags=["submission"])


@router.post("/start", response_model=SubmissionStartResponse)
async def start_submission(
    team: Annotated[Team, Depends(get_current_team)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
    settings_service: Annotated[SettingsService, Depends(get_settings_service)],
) -> SubmissionStartResponse:
    """Starts a new submission run for the authenticated team."""
    service = SubmissionService(session, settings_service)
    res = await service.start_submission(team.team_id)
    return SubmissionStartResponse(**res)


@router.get("/{submission_id}/status", response_model=SubmissionStatusResponse)
async def get_submission_status(
    submission_id: uuid.UUID,
    team: Annotated[Team, Depends(get_current_team)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
    settings_service: Annotated[SettingsService, Depends(get_settings_service)],
) -> SubmissionStatusResponse:
    """Gets the status, progress, and remaining time of a submission."""
    service = SubmissionService(session, settings_service)
    res = await service.get_submission_status(team.team_id, submission_id)
    return SubmissionStatusResponse(**res)


@router.post("/{submission_id}/finalize", response_model=SubmissionFinalizeResponse)
async def finalize_submission(
    submission_id: uuid.UUID,
    team: Annotated[Team, Depends(get_current_team)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
    settings_service: Annotated[SettingsService, Depends(get_settings_service)],
) -> SubmissionFinalizeResponse:
    """Finalizes an in-progress submission to completed state."""
    service = SubmissionService(session, settings_service)
    res = await service.finalize_submission(team.team_id, submission_id)
    return SubmissionFinalizeResponse(**res)
