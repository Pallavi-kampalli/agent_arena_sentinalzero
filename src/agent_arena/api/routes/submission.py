import uuid
from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from agent_arena.api.deps import get_current_team, get_db_session, get_settings_service
from agent_arena.models.team import Team
from agent_arena.schemas.submission import (
    BatchSubmissionSubmitRequest,
    BatchSubmissionSubmitResponse,
    SubmissionAbortResponse,
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
    """Starts a new submission run for the authenticated team, returning all tasks in randomized order."""
    service = SubmissionService(session, settings_service)
    res = await service.start_submission(team.team_id)
    return SubmissionStartResponse(**res)


@router.post("/{submission_id}/submit", response_model=BatchSubmissionSubmitResponse)
@router.post("/{submission_id}/submit_batch", response_model=BatchSubmissionSubmitResponse)
async def submit_batch(
    submission_id: uuid.UUID,
    req: BatchSubmissionSubmitRequest,
    team: Annotated[Team, Depends(get_current_team)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
    settings_service: Annotated[SettingsService, Depends(get_settings_service)],
) -> BatchSubmissionSubmitResponse:
    """Submits all answers for a submission epoch at once, calculates inter-task timing, and scores."""
    service = SubmissionService(session, settings_service)
    res = await service.submit_batch(team.team_id, submission_id, req.answers)
    return BatchSubmissionSubmitResponse(**res)


@router.post("/{submission_id}/abort", response_model=SubmissionAbortResponse)
async def abort_submission(
    submission_id: uuid.UUID,
    team: Annotated[Team, Depends(get_current_team)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
    settings_service: Annotated[SettingsService, Depends(get_settings_service)],
) -> SubmissionAbortResponse:
    """Aborts an active in-progress submission, marking it interrupted so it is not recorded or scored."""
    service = SubmissionService(session, settings_service)
    res = await service.abort_submission(team.team_id, submission_id)
    return SubmissionAbortResponse(**res)


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
