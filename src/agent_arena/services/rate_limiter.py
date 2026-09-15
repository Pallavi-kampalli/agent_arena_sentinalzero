import uuid
from datetime import UTC, datetime, timedelta

import sqlalchemy as sa
from fastapi import HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from agent_arena.models.tool_call_log import ToolCallLog
from agent_arena.services.settings_service import SettingsService


class ToolLimiter:
    """Enforces dynamic team rate limits and task tool call budgets."""

    @staticmethod
    async def check_rate_limit(
        session: AsyncSession,
        team_id: uuid.UUID,
        settings_service: SettingsService,
    ) -> None:
        """Enforces rate_limit_tool_calls_per_min from settings."""
        limit = await settings_service.get("rate_limit_tool_calls_per_min")
        if limit is None:
            limit = 60

        if limit <= 0:
            return

        window_start = datetime.now(UTC) - timedelta(seconds=60)
        stmt = (
            sa.select(sa.func.count())
            .select_from(ToolCallLog)
            .where(
                ToolCallLog.team_id == team_id,
                ToolCallLog.created_at >= window_start,
            )
        )
        call_count = (await session.execute(stmt)).scalar() or 0

        if call_count >= limit:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail={
                    "error": "RATE_LIMIT_EXCEEDED",
                    "message": f"Rate limit of {limit} tool calls per minute exceeded for this team.",
                },
            )

    @staticmethod
    async def check_task_budget(
        session: AsyncSession,
        team_id: uuid.UUID,
        task_id: str,
        settings_service: SettingsService,
    ) -> None:
        """Enforces tool_call_budget_per_task from settings."""
        budget = await settings_service.get("tool_call_budget_per_task")
        if budget is None:
            budget = 40

        if budget <= 0:
            return

        stmt = (
            sa.select(sa.func.count())
            .select_from(ToolCallLog)
            .where(
                ToolCallLog.team_id == team_id,
                ToolCallLog.task_id == task_id,
            )
        )
        call_count = (await session.execute(stmt)).scalar() or 0

        if call_count >= budget:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail={
                    "error": "BUDGET_EXCEEDED",
                    "message": f"Tool call budget of {budget} calls exceeded for task '{task_id}'.",
                },
            )

    @staticmethod
    async def check_limits(
        session: AsyncSession,
        team_id: uuid.UUID,
        task_id: str,
        settings_service: SettingsService,
    ) -> None:
        """Consolidated check for both rate limits and task budgets in a single database query."""
        rate_limit = await settings_service.get("rate_limit_tool_calls_per_min")
        if rate_limit is None:
            rate_limit = 60

        budget = await settings_service.get("tool_call_budget_per_task")
        if budget is None:
            budget = 40

        if rate_limit <= 0 and budget <= 0:
            return

        window_start = datetime.now(UTC) - timedelta(seconds=60)
        stmt = (
            sa.select(
                sa.func.count().filter(ToolCallLog.created_at >= window_start).label("rate_count"),
                sa.func.count().filter(ToolCallLog.task_id == task_id).label("budget_count"),
            )
            .select_from(ToolCallLog)
            .where(ToolCallLog.team_id == team_id)
        )
        row = (await session.execute(stmt)).one()
        rate_count = row.rate_count or 0
        budget_count = row.budget_count or 0

        # Prioritize budget check if budget is reached
        if budget > 0 and budget_count >= budget:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail={
                    "error": "BUDGET_EXCEEDED",
                    "message": f"Tool call budget of {budget} calls exceeded for task '{task_id}'.",
                },
            )

        if rate_limit > 0 and rate_count >= rate_limit:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail={
                    "error": "RATE_LIMIT_EXCEEDED",
                    "message": f"Rate limit of {rate_limit} tool calls per minute exceeded for this team.",
                },
            )
