from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header
from sqlalchemy.ext.asyncio import AsyncSession

from agent_arena.api.deps import get_current_team, get_db_session, get_settings_service
from agent_arena.models.team import Team
from agent_arena.schemas.tools import (
    CancelSubscriptionRequest,
    EscalateCaseRequest,
    GetCustomerRequest,
    GetDocumentRequest,
    GetPreviousCasesRequest,
    GetSubscriptionRequest,
    GetTransactionsRequest,
    IssueRefundRequest,
    RequestVerificationRequest,
    SearchKnowledgeRequest,
)
from agent_arena.services.settings_service import SettingsService
from agent_arena.services.tool_service import ToolService

router = APIRouter(prefix="/tools", tags=["tools"])


# --- Read Tools (6 endpoints) ---


@router.post("/search_knowledge")
async def search_knowledge(
    req: SearchKnowledgeRequest,
    team: Annotated[Team, Depends(get_current_team)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
    settings_service: Annotated[SettingsService, Depends(get_settings_service)],
    x_task_id: Annotated[str | None, Header(alias="X-Task-ID")] = None,
) -> dict[str, Any]:
    """Searches knowledge base (policies, documents) for the current task."""
    service = ToolService(session, settings_service)
    return await service.run_tool(team, "search_knowledge", req.model_dump(), is_action=False, task_id=x_task_id)


@router.post("/get_document")
async def get_document(
    req: GetDocumentRequest,
    team: Annotated[Team, Depends(get_current_team)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
    settings_service: Annotated[SettingsService, Depends(get_settings_service)],
    x_task_id: Annotated[str | None, Header(alias="X-Task-ID")] = None,
) -> dict[str, Any]:
    """Retrieves full document or policy by ID within the current task."""
    service = ToolService(session, settings_service)
    return await service.run_tool(team, "get_document", req.model_dump(), is_action=False, task_id=x_task_id)


@router.post("/get_customer")
async def get_customer(
    req: GetCustomerRequest,
    team: Annotated[Team, Depends(get_current_team)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
    settings_service: Annotated[SettingsService, Depends(get_settings_service)],
    x_task_id: Annotated[str | None, Header(alias="X-Task-ID")] = None,
) -> dict[str, Any]:
    """Retrieves customer record within the current task."""
    service = ToolService(session, settings_service)
    return await service.run_tool(team, "get_customer", req.model_dump(), is_action=False, task_id=x_task_id)


@router.post("/get_transactions")
async def get_transactions(
    req: GetTransactionsRequest,
    team: Annotated[Team, Depends(get_current_team)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
    settings_service: Annotated[SettingsService, Depends(get_settings_service)],
    x_task_id: Annotated[str | None, Header(alias="X-Task-ID")] = None,
) -> dict[str, Any]:
    """Retrieves customer transaction history with optional date range."""
    service = ToolService(session, settings_service)
    return await service.run_tool(team, "get_transactions", req.model_dump(), is_action=False, task_id=x_task_id)


@router.post("/get_subscription")
async def get_subscription(
    req: GetSubscriptionRequest,
    team: Annotated[Team, Depends(get_current_team)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
    settings_service: Annotated[SettingsService, Depends(get_settings_service)],
    x_task_id: Annotated[str | None, Header(alias="X-Task-ID")] = None,
) -> dict[str, Any]:
    """Retrieves customer subscription within the current task."""
    service = ToolService(session, settings_service)
    return await service.run_tool(team, "get_subscription", req.model_dump(), is_action=False, task_id=x_task_id)


@router.post("/get_previous_cases")
async def get_previous_cases(
    req: GetPreviousCasesRequest,
    team: Annotated[Team, Depends(get_current_team)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
    settings_service: Annotated[SettingsService, Depends(get_settings_service)],
    x_task_id: Annotated[str | None, Header(alias="X-Task-ID")] = None,
) -> dict[str, Any]:
    """Retrieves historical ticket cases for customer within the current task."""
    service = ToolService(session, settings_service)
    return await service.run_tool(team, "get_previous_cases", req.model_dump(), is_action=False, task_id=x_task_id)


# --- Action Tools (4 endpoints, server-side enforced) ---


@router.post("/issue_refund")
async def issue_refund(
    req: IssueRefundRequest,
    team: Annotated[Team, Depends(get_current_team)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
    settings_service: Annotated[SettingsService, Depends(get_settings_service)],
    x_task_id: Annotated[str | None, Header(alias="X-Task-ID")] = None,
) -> dict[str, Any]:
    """Refunds a transaction with server-side eligibility enforcement.

    Always returns HTTP 200 on valid requests; policy rejection returns { error: 'INELIGIBLE', ... }.
    """
    service = ToolService(session, settings_service)
    return await service.run_tool(team, "issue_refund", req.model_dump(), is_action=True, task_id=x_task_id)


@router.post("/cancel_subscription")
async def cancel_subscription(
    req: CancelSubscriptionRequest,
    team: Annotated[Team, Depends(get_current_team)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
    settings_service: Annotated[SettingsService, Depends(get_settings_service)],
    x_task_id: Annotated[str | None, Header(alias="X-Task-ID")] = None,
) -> dict[str, Any]:
    """Cancels a subscription with server-side lock-in and dispute enforcement."""
    service = ToolService(session, settings_service)
    return await service.run_tool(team, "cancel_subscription", req.model_dump(), is_action=True, task_id=x_task_id)


@router.post("/escalate_case")
async def escalate_case(
    req: EscalateCaseRequest,
    team: Annotated[Team, Depends(get_current_team)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
    settings_service: Annotated[SettingsService, Depends(get_settings_service)],
    x_task_id: Annotated[str | None, Header(alias="X-Task-ID")] = None,
) -> dict[str, Any]:
    """Escalates a case to a specialized team with evidence grounding verification."""
    service = ToolService(session, settings_service)
    return await service.run_tool(team, "escalate_case", req.model_dump(), is_action=True, task_id=x_task_id)


@router.post("/request_verification")
async def request_verification(
    req: RequestVerificationRequest,
    team: Annotated[Team, Depends(get_current_team)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
    settings_service: Annotated[SettingsService, Depends(get_settings_service)],
    x_task_id: Annotated[str | None, Header(alias="X-Task-ID")] = None,
) -> dict[str, Any]:
    """Initiates secondary customer verification challenge (safe fallback)."""
    service = ToolService(session, settings_service)
    return await service.run_tool(team, "request_verification", req.model_dump(), is_action=True, task_id=x_task_id)
