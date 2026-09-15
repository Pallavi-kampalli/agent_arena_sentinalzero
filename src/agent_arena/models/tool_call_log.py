import uuid
from datetime import datetime
from typing import Any, Optional
import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from agent_arena.models.base import Base, PortableJSON, PortableUUID, utc_now


class ToolCallLog(Base):
    __tablename__ = "tool_call_logs"

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
    task_id: Mapped[Optional[str]] = mapped_column(sa.Text, nullable=True, index=True)
    submission_id: Mapped[Optional[uuid.UUID]] = mapped_column(PortableUUID, nullable=True, index=True)
    tool_name: Mapped[str] = mapped_column(sa.Text, nullable=False, index=True)
    request_payload: Mapped[Optional[dict[str, Any]]] = mapped_column(PortableJSON, nullable=True)  # never includes bearer token
    response_payload: Mapped[Optional[dict[str, Any]]] = mapped_column(PortableJSON, nullable=True)
    was_enforcement_rejection: Mapped[bool] = mapped_column(sa.Boolean, default=False, nullable=False)
    latency_ms: Mapped[int] = mapped_column(sa.Integer, default=0, nullable=False)
    created_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), default=utc_now, nullable=False, index=True)
