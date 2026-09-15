import uuid
from datetime import datetime
from typing import Any, Optional
import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column, relationship

from agent_arena.models.base import Base, PortableJSON, PortableUUID, utc_now


class TaskAssignment(Base):
    __tablename__ = "task_assignments"

    id: Mapped[int] = mapped_column(
        sa.BigInteger().with_variant(sa.Integer, "sqlite"),
        primary_key=True,
        autoincrement=True,
    )
    team_id: Mapped[uuid.UUID] = mapped_column(
        PortableUUID,
        sa.ForeignKey("teams.team_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    task_id: Mapped[str] = mapped_column(
        sa.Text,
        sa.ForeignKey("tasks.task_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    submission_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        PortableUUID,
        sa.ForeignKey("submissions.submission_id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    assigned_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), default=utc_now, nullable=False)
    world_runtime_state: Mapped[dict[str, Any]] = mapped_column(PortableJSON, nullable=False)

    # Relationships
    team = relationship("Team", back_populates="task_assignments")
    task = relationship("Task", back_populates="assignments")
    submission = relationship("Submission", back_populates="task_assignments")
