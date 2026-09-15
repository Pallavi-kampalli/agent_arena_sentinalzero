from fastapi import APIRouter, Depends

from agent_arena.api.deps import get_current_team
from agent_arena.models.team import Team

router = APIRouter(prefix="/team", tags=["Team"])


@router.get("/me")
async def get_my_team(current_team: Team = Depends(get_current_team)):
    return {
        "team_id": str(current_team.team_id),
        "team_name": current_team.team_name,
        "status": current_team.status,
        "token_version": current_team.token_version,
    }
