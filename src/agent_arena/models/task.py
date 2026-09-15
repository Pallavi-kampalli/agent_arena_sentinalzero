from datetime import datetime
from typing import Any

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column, relationship

from agent_arena.models.base import Base, PortableJSON, utc_now


class Task(Base):
    __tablename__ = "tasks"

    task_id: Mapped[str] = mapped_column(sa.Text, primary_key=True)
    dataset: Mapped[str] = mapped_column(sa.Text, nullable=False, index=True)  # 'dev' | 'hidden'
    family: Mapped[str] = mapped_column(sa.Text, nullable=False, index=True)  # one of the 6 SupportOps task families
    variant: Mapped[str] = mapped_column(
        sa.Text, nullable=False
    )  # normal | distractor | contradiction | missing_info | adversarial | stale
    input_payload: Mapped[dict[str, Any]] = mapped_column(PortableJSON, nullable=False)  # customer_message, customer_id
    world_state_seed: Mapped[dict[str, Any]] = mapped_column(PortableJSON, nullable=False)
    ground_truth: Mapped[dict[str, Any]] = mapped_column(PortableJSON, nullable=False)  # NEVER exposed in prod mode
    created_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), default=utc_now, nullable=False)

    # Relationships
    assignments = relationship("TaskAssignment", back_populates="task", cascade="all, delete-orphan")
