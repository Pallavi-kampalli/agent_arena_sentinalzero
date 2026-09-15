from fastapi import APIRouter, Depends
from agent_arena.api.deps import get_settings_service
from agent_arena.config import get_config
from agent_arena.services.settings_service import SettingsService

router = APIRouter()


@router.get("/health", tags=["Health"])
async def health_check(settings: SettingsService = Depends(get_settings_service)):
    phase = await settings.get("competition_phase", "registration")
    config = get_config()
    return {
        "status": "ok",
        "phase": phase,
        "environment": config.ENVIRONMENT,
    }
