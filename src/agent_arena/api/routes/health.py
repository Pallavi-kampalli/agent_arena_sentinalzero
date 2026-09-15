import sqlalchemy as sa
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from agent_arena.api.deps import get_db_session
from agent_arena.config import get_config
from agent_arena.services.settings_service import SettingsService

router = APIRouter()


@router.get("/health", tags=["Health"])
async def health_check(session: AsyncSession = Depends(get_db_session)):
    """Liveness probe: returns 200 if API process is running."""
    config = get_config()
    phase = "registration"
    try:
        settings = SettingsService(session)
        phase = await settings.get("competition_phase", "registration")
    except Exception:
        pass
    return {
        "status": "ok",
        "phase": phase,
        "environment": config.ENVIRONMENT,
    }


@router.get("/health/ready", tags=["Health"])
async def readiness_check(session: AsyncSession = Depends(get_db_session)):
    """Readiness probe: validates database connectivity via SELECT 1."""
    config = get_config()
    try:
        await session.execute(sa.text("SELECT 1"))
        return {
            "status": "ready",
            "database": "connected",
            "environment": config.ENVIRONMENT,
        }
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "status": "not_ready",
                "database": "disconnected",
                "error": str(e),
            },
        )
