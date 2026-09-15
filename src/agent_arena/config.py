from functools import lru_cache
from typing import Literal
from pydantic_settings import BaseSettings, SettingsConfigDict


class AppConfig(BaseSettings):
    DATABASE_URL: str = "postgresql+asyncpg://postgres:postgres@localhost:5432/agent_arena"
    ADMIN_PANEL_SECRET: str = "dev-admin-secret-key-32-chars-min-for-agent-arena"
    JWT_SIGNING_SECRET: str = "dev-jwt-secret-key-32-chars-min-for-agent-arena"
    PORT: int = 8000
    ENVIRONMENT: Literal["production", "staging"] = "staging"
    REVEAL_GROUND_TRUTH: bool = False

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )


@lru_cache
def get_config() -> AppConfig:
    return AppConfig()
