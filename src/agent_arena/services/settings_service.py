import json
from pathlib import Path
from typing import Any

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from agent_arena.logging import logger
from agent_arena.models.setting import Setting, SettingsAuditLog
from agent_arena.schemas.settings import validate_setting_value


class SettingsService:
    """Manages competition-level settings with validation, caching, and audit logging.

    All competition-tunable parameters MUST be read from this service rather than
    being hardcoded in any downstream modules (PRD §2.2, §12).
    """

    _cache: dict[str, Any] = {}
    _defaults: dict[str, Any] = {}
    _initialized: bool = False

    def __init__(self, session: AsyncSession | None = None):
        self.session = session
        self._load_defaults_file()

    @classmethod
    def _load_defaults_file(cls) -> dict[str, Any]:
        if not cls._defaults:
            defaults_path = Path(__file__).parent.parent / "settings_defaults.json"
            if defaults_path.exists():
                with open(defaults_path, "r", encoding="utf-8") as f:
                    cls._defaults = json.load(f)
            else:
                logger.warning(f"Settings defaults file not found at {defaults_path}")
        return cls._defaults

    @classmethod
    def get_cached(cls, key: str, default: Any = None) -> Any:
        """Read directly from in-memory cache without hitting DB (fast path)."""
        if key in cls._cache:
            return cls._cache[key]
        if key in cls._defaults:
            return cls._defaults[key]
        return default

    async def get(self, key: str, default: Any = None) -> Any:
        """Get a setting by key. Checks cache, then DB, then defaults file."""
        if key in self._cache:
            return self._cache[key]

        if self.session is not None:
            result = await self.session.execute(sa.select(Setting).where(Setting.key == key))
            setting = result.scalar_one_or_none()
            if setting is not None:
                self._cache[key] = setting.value
                return setting.value

        # Fallback to defaults
        if key in self._defaults:
            return self._defaults[key]
        return default

    async def get_all(self) -> dict[str, Any]:
        """Returns a merged dictionary of all current settings."""
        result_dict = dict(self._defaults)
        if self.session is not None:
            result = await self.session.execute(sa.select(Setting))
            for setting in result.scalars().all():
                result_dict[setting.key] = setting.value
                self._cache[setting.key] = setting.value
        return result_dict

    async def set(self, key: str, value: Any, changed_by: str = "admin") -> Any:
        """Validates, persists to DB, logs to audit table, and updates cache."""
        if self.session is None:
            raise ValueError("Database session required to set a setting")

        # Get existing value for audit and phase-transition validation
        result = await self.session.execute(sa.select(Setting).where(Setting.key == key))
        existing_setting = result.scalar_one_or_none()
        old_value = existing_setting.value if existing_setting else self._defaults.get(key)

        # Validate setting value using Pydantic schema rules
        validated_value = validate_setting_value(key, value, current_value=old_value)

        # Save or update setting
        if existing_setting is not None:
            existing_setting.value = validated_value
            existing_setting.updated_by = changed_by
        else:
            new_setting = Setting(key=key, value=validated_value, updated_by=changed_by)
            self.session.add(new_setting)

        # Write audit log entry (PRD §2.2, §3)
        audit_log = SettingsAuditLog(
            key=key,
            old_value=old_value,
            new_value=validated_value,
            changed_by=changed_by,
        )
        self.session.add(audit_log)
        await self.session.commit()

        # Update cache
        self._cache[key] = validated_value
        logger.info(
            f"Setting '{key}' updated by {changed_by}",
            extra={"key": key, "changed_by": changed_by, "old_value": old_value, "new_value": validated_value},
        )
        return validated_value

    async def seed_defaults(self, changed_by: str = "system") -> None:
        """Seeds any missing defaults from settings_defaults.json into PostgreSQL."""
        if self.session is None:
            raise ValueError("Database session required to seed defaults")

        self._load_defaults_file()
        result = await self.session.execute(sa.select(Setting))
        existing_keys = {s.key: s.value for s in result.scalars().all()}

        seeded_any = False
        for key, default_val in self._defaults.items():
            if key not in existing_keys:
                setting = Setting(key=key, value=default_val, updated_by=changed_by)
                self.session.add(setting)
                audit_log = SettingsAuditLog(
                    key=key,
                    old_value=None,
                    new_value=default_val,
                    changed_by=changed_by,
                )
                self.session.add(audit_log)
                self._cache[key] = default_val
                seeded_any = True
            else:
                self._cache[key] = existing_keys[key]

        if seeded_any:
            await self.session.commit()
            logger.info("Seeded missing default settings into database")
