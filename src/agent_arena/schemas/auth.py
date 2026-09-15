import uuid
from typing import Optional
from pydantic import BaseModel


class TokenClaims(BaseModel):
    sub: str  # team_id UUID as string
    version: int
    iat: int
    exp: Optional[int] = None
