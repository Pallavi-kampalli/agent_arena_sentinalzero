from datetime import datetime
from typing import Any

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from agent_arena.models.base import Base, PortableJSON, utc_now


class Setting(Base):
    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(sa.Text, primary_key=True)
    value: Mapped[Any] = mapped_column(PortableJSON, nullable=False)
    updated_by: Mapped[str] = mapped_column(sa.Text, default="system", nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True),
        default=utc_now,
        onupdate=utc_now,
        nullable=False,
    )


class SettingsAuditLog(Base):
    __tablename__ = "settings_audit_log"

    id: Mapped[int] = mapped_column(
        sa.BigInteger().with_variant(sa.Integer, "sqlite"),
        primary_key=True,
        autoincrement=True,
    )
    key: Mapped[str] = mapped_column(sa.Text, nullable=False, index=True)
    old_value: Mapped[Any | None] = mapped_column(PortableJSON, nullable=True)
    new_value: Mapped[Any] = mapped_column(PortableJSON, nullable=False)
    changed_by: Mapped[str] = mapped_column(sa.Text, nullable=False)
    changed_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True),
        default=utc_now,
        nullable=False,
        index=True,
    )
