import uuid
from datetime import datetime
from typing import Any, Optional
import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column, relationship

from agent_arena.models.base import Base, PortableJSON, PortableUUID, utc_now


class Submission(Base):
    __tablename__ = "submissions"

    submission_id: Mapped[uuid.UUID] = mapped_column(
        PortableUUID,
        primary_key=True,
        default=uuid.uuid4,
    )
    team_id: Mapped[uuid.UUID] = mapped_column(
        PortableUUID,
        sa.ForeignKey("teams.team_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    attempt_number: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    started_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), default=utc_now, nullable=False)
    completed_at: Mapped[Optional[datetime]] = mapped_column(sa.DateTime(timezone=True), nullable=True)
    status: Mapped[str] = mapped_column(sa.Text, default="in_progress", nullable=False)  # in_progress | completed | expired
    per_task_results: Mapped[Optional[list[dict[str, Any]]]] = mapped_column(PortableJSON, nullable=True)
    aggregate_score: Mapped[Optional[float]] = mapped_column(sa.Numeric(precision=8, scale=4), nullable=True)
    breakdown: Mapped[Optional[dict[str, Any]]] = mapped_column(PortableJSON, nullable=True)

    # Relationships
    team = relationship("Team", back_populates="submissions")
    task_assignments = relationship("TaskAssignment", back_populates="submission")
