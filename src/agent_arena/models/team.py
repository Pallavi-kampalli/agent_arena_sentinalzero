import uuid
from datetime import datetime
from typing import Any

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column, relationship

from agent_arena.models.base import Base, PortableJSON, PortableUUID, utc_now


class Team(Base):
    __tablename__ = "teams"

    team_id: Mapped[uuid.UUID] = mapped_column(
        PortableUUID,
        primary_key=True,
        default=uuid.uuid4,
    )
    team_name: Mapped[str] = mapped_column(sa.Text, nullable=False)
    display_id: Mapped[int | None] = mapped_column(sa.Integer, unique=True, index=True, nullable=True)
    team_code: Mapped[str | None] = mapped_column(sa.Text, unique=True, index=True, nullable=True)
    members: Mapped[dict[str, Any] | list[Any] | None] = mapped_column(PortableJSON, nullable=True)
    github_repo_url: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    bearer_token_hash: Mapped[str] = mapped_column(sa.Text, nullable=False, index=True)
    token_version: Mapped[int] = mapped_column(sa.Integer, default=1, nullable=False)
    status: Mapped[str] = mapped_column(sa.Text, default="active", nullable=False)  # active | disqualified | suspended
    created_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), default=utc_now, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False
    )

    # Relationships
    submissions = relationship("Submission", back_populates="team", cascade="all, delete-orphan")
    task_assignments = relationship("TaskAssignment", back_populates="team", cascade="all, delete-orphan")
