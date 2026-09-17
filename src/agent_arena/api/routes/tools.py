from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header
from sqlalchemy.ext.asyncio import AsyncSession

from agent_arena.api.deps import get_current_team, get_db_session, get_settings_service
from agent_arena.models.team import Team
from agent_arena.schemas.tools import (
    AllowAndDeliverRequest,
    ApplyWarningBannerRequest,
    EscalateToTier2SocRequest,
    GetApprovedDomainsRequest,
    GetEmailHeadersRequest,
    GetThreadHistoryRequest,
    InspectDomainReputationRequest,
    LookupDirectoryRequest,
    QuarantineMessageRequest,
)
from agent_arena.services.settings_service import SettingsService
from agent_arena.services.tool_service import ToolService

router = APIRouter(prefix="/tools", tags=["tools"])


# --- SentinelZero Read Tools (5 endpoints) ---


@router.post("/lookup_directory")
async def lookup_directory(
    req: LookupDirectoryRequest,
    team: Annotated[Team, Depends(get_current_team)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
    settings_service: Annotated[SettingsService, Depends(get_settings_service)],
    x_task_id: Annotated[str | None, Header(alias="X-Task-ID")] = None,
) -> dict[str, Any]:
    """Looks up employee identity details in the organization directory."""
    service = ToolService(session, settings_service)
    return await service.run_tool(team, "lookup_directory", req.model_dump(), is_action=False, task_id=x_task_id)


@router.post("/get_approved_domains")
async def get_approved_domains(
    req: GetApprovedDomainsRequest,
    team: Annotated[Team, Depends(get_current_team)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
    settings_service: Annotated[SettingsService, Depends(get_settings_service)],
    x_task_id: Annotated[str | None, Header(alias="X-Task-ID")] = None,
) -> dict[str, Any]:
    """Retrieves official organization domains and trusted partner domains."""
    service = ToolService(session, settings_service)
    return await service.run_tool(team, "get_approved_domains", req.model_dump(), is_action=False, task_id=x_task_id)


@router.post("/get_email_headers")
async def get_email_headers(
    req: GetEmailHeadersRequest,
    team: Annotated[Team, Depends(get_current_team)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
    settings_service: Annotated[SettingsService, Depends(get_settings_service)],
    x_task_id: Annotated[str | None, Header(alias="X-Task-ID")] = None,
) -> dict[str, Any]:
    """Retrieves email authentication and security header information."""
    service = ToolService(session, settings_service)
    return await service.run_tool(team, "get_email_headers", req.model_dump(), is_action=False, task_id=x_task_id)


@router.post("/inspect_domain_reputation")
async def inspect_domain_reputation(
    req: InspectDomainReputationRequest,
    team: Annotated[Team, Depends(get_current_team)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
    settings_service: Annotated[SettingsService, Depends(get_settings_service)],
    x_task_id: Annotated[str | None, Header(alias="X-Task-ID")] = None,
) -> dict[str, Any]:
    """Inspects threat intelligence and reputation for a domain."""
    service = ToolService(session, settings_service)
    return await service.run_tool(team, "inspect_domain_reputation", req.model_dump(), is_action=False, task_id=x_task_id)


@router.post("/get_thread_history")
async def get_thread_history(
    req: GetThreadHistoryRequest,
    team: Annotated[Team, Depends(get_current_team)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
    settings_service: Annotated[SettingsService, Depends(get_settings_service)],
    x_task_id: Annotated[str | None, Header(alias="X-Task-ID")] = None,
) -> dict[str, Any]:
    """Retrieves chronological thread history for multi-turn email conversations."""
    service = ToolService(session, settings_service)
    return await service.run_tool(team, "get_thread_history", req.model_dump(), is_action=False, task_id=x_task_id)


# --- SentinelZero Action Tools (4 endpoints) ---


@router.post("/allow_and_deliver")
async def allow_and_deliver(
    req: AllowAndDeliverRequest,
    team: Annotated[Team, Depends(get_current_team)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
    settings_service: Annotated[SettingsService, Depends(get_settings_service)],
    x_task_id: Annotated[str | None, Header(alias="X-Task-ID")] = None,
) -> dict[str, Any]:
    """Delivers the message normally (ALLOW decision)."""
    service = ToolService(session, settings_service)
    return await service.run_tool(team, "allow_and_deliver", req.model_dump(), is_action=True, task_id=x_task_id)


@router.post("/apply_warning_banner")
async def apply_warning_banner(
    req: ApplyWarningBannerRequest,
    team: Annotated[Team, Depends(get_current_team)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
    settings_service: Annotated[SettingsService, Depends(get_settings_service)],
    x_task_id: Annotated[str | None, Header(alias="X-Task-ID")] = None,
) -> dict[str, Any]:
    """Applies a security warning banner to the message (WARN decision)."""
    service = ToolService(session, settings_service)
    return await service.run_tool(team, "apply_warning_banner", req.model_dump(), is_action=True, task_id=x_task_id)


@router.post("/quarantine_message")
async def quarantine_message(
    req: QuarantineMessageRequest,
    team: Annotated[Team, Depends(get_current_team)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
    settings_service: Annotated[SettingsService, Depends(get_settings_service)],
    x_task_id: Annotated[str | None, Header(alias="X-Task-ID")] = None,
) -> dict[str, Any]:
    """Quarantines the message (QUARANTINE decision)."""
    service = ToolService(session, settings_service)
    return await service.run_tool(team, "quarantine_message", req.model_dump(), is_action=True, task_id=x_task_id)


@router.post("/escalate_to_tier2_soc")
async def escalate_to_tier2_soc(
    req: EscalateToTier2SocRequest,
    team: Annotated[Team, Depends(get_current_team)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
    settings_service: Annotated[SettingsService, Depends(get_settings_service)],
    x_task_id: Annotated[str | None, Header(alias="X-Task-ID")] = None,
) -> dict[str, Any]:
    """Escalates the incident to human Tier-2 SOC review (ESCALATE decision)."""
    service = ToolService(session, settings_service)
    return await service.run_tool(team, "escalate_to_tier2_soc", req.model_dump(), is_action=True, task_id=x_task_id)
