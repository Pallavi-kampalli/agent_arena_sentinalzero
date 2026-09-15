import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class TeamRegisterRequest(BaseModel):
    team_name: str = Field(..., min_length=2, max_length=100)
    members: Any | None = None
    github_repo_url: str | None = None


class TeamResponse(BaseModel):
    team_id: uuid.UUID
    team_name: str
    members: Any | None = None
    github_repo_url: str | None = None
    token_version: int
    status: str
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class TeamRegisterResponse(BaseModel):
    team_id: uuid.UUID
    team_name: str
    bearer_token: str
    env_snippet: str
