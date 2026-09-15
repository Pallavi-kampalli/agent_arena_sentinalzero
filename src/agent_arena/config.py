import math
from collections import Counter
from functools import lru_cache
from typing import Literal

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


def calculate_shannon_entropy(s: str) -> float:
    """Calculates Shannon entropy in bits per character."""
    if not s:
        return 0.0
    counts = Counter(s)
    length = float(len(s))
    return -sum((count / length) * math.log2(count / length) for count in counts.values())


class AppConfig(BaseSettings):
    DATABASE_URL: str = "postgresql+psycopg://postgres:postgrespassword@127.0.0.1:5432/agent_arena"
    ADMIN_PANEL_SECRET: str = "dev-admin-secret-key-32-chars-min-for-agent-arena"
    JWT_SIGNING_SECRET: str = "dev-jwt-secret-key-32-chars-min-for-agent-arena"
    PORT: int = 8000
    ENVIRONMENT: Literal["production", "staging"] = "staging"
    REVEAL_GROUND_TRUTH: bool = False
    CORS_ORIGINS: str = "*"
    ALLOWED_HOSTS: str = "*"
    LOG_LEVEL: str = "INFO"

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    @property
    def cors_origins_list(self) -> list[str]:
        return [o.strip() for o in self.CORS_ORIGINS.split(",") if o.strip()]

    @property
    def allowed_hosts_list(self) -> list[str]:
        return [h.strip() for h in self.ALLOWED_HOSTS.split(",") if h.strip()]

    @model_validator(mode="after")
    def validate_production_configuration(self) -> "AppConfig":
        if self.ENVIRONMENT == "production":
            insecure_placeholders = {
                "change-this",
                "dev-admin",
                "dev-jwt",
                "secret-key",
                "default",
                "example",
                "password",
            }

            # 1. Validate ADMIN_PANEL_SECRET
            if len(self.ADMIN_PANEL_SECRET) < 32:
                raise ValueError("ADMIN_PANEL_SECRET must be at least 32 characters long in production.")
            if any(ph in self.ADMIN_PANEL_SECRET.lower() for ph in insecure_placeholders):
                raise ValueError("ADMIN_PANEL_SECRET contains insecure development placeholder text.")
            if calculate_shannon_entropy(self.ADMIN_PANEL_SECRET) < 3.0:
                raise ValueError(
                    "ADMIN_PANEL_SECRET entropy is too low (< 3.0 bits/char). Use a genuine cryptographic secret."
                )
            if len(set(self.ADMIN_PANEL_SECRET)) < 10:
                raise ValueError("ADMIN_PANEL_SECRET lacks sufficient unique character diversity (< 10 unique chars).")

            # 2. Validate JWT_SIGNING_SECRET
            if len(self.JWT_SIGNING_SECRET) < 32:
                raise ValueError("JWT_SIGNING_SECRET must be at least 32 characters long in production.")
            if any(ph in self.JWT_SIGNING_SECRET.lower() for ph in insecure_placeholders):
                raise ValueError("JWT_SIGNING_SECRET contains insecure development placeholder text.")
            if calculate_shannon_entropy(self.JWT_SIGNING_SECRET) < 3.0:
                raise ValueError(
                    "JWT_SIGNING_SECRET entropy is too low (< 3.0 bits/char). Use a genuine cryptographic secret."
                )
            if len(set(self.JWT_SIGNING_SECRET)) < 10:
                raise ValueError("JWT_SIGNING_SECRET lacks sufficient unique character diversity (< 10 unique chars).")

            # 3. Validate DATABASE_URL
            if "sqlite" in self.DATABASE_URL.lower():
                raise ValueError("SQLite is strictly forbidden in production mode.")
            if not self.DATABASE_URL.lower().startswith("postgresql"):
                raise ValueError("DATABASE_URL must target a PostgreSQL database in production mode.")

            # 4. Validate CORS_ORIGINS
            if self.CORS_ORIGINS.strip() == "*" or "*" in self.cors_origins_list:
                raise ValueError("Wildcard CORS ('*') is strictly forbidden in production mode.")

            # 5. Validate REVEAL_GROUND_TRUTH
            if self.REVEAL_GROUND_TRUTH:
                raise ValueError("REVEAL_GROUND_TRUTH must be False in production mode.")

        return self


@lru_cache
def get_config() -> AppConfig:
    return AppConfig()
