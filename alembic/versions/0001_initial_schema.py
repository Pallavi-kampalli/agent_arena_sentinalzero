"""0001_initial_schema

Revision ID: 0001
Revises:
Create Date: 2026-09-15 00:00:00.000000

"""

from typing import Sequence, Union

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Dialect-portable types
PortableJSON = postgresql.JSONB().with_variant(sa.JSON(), "sqlite")
PortableUUID = sa.Uuid(as_uuid=True).with_variant(postgresql.UUID(as_uuid=True), "postgresql")


def upgrade() -> None:
    # 1. teams table
    op.create_table(
        "teams",
        sa.Column("team_id", PortableUUID, primary_key=True),
        sa.Column("display_id", sa.Integer(), nullable=True),
        sa.Column("team_code", sa.Text(), nullable=True),
        sa.Column("team_name", sa.Text(), nullable=False),
        sa.Column("members", PortableJSON, nullable=True),
        sa.Column("github_repo_url", sa.Text(), nullable=True),
        sa.Column("bearer_token_hash", sa.Text(), nullable=False),
        sa.Column("token_version", sa.Integer(), server_default="1", nullable=False),
        sa.Column("status", sa.Text(), server_default="active", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_teams_bearer_token_hash", "teams", ["bearer_token_hash"])
    op.create_index("ix_teams_team_code", "teams", ["team_code"])

    # 2. tasks table
    op.create_table(
        "tasks",
        sa.Column("task_id", sa.Text(), primary_key=True),
        sa.Column("dataset", sa.Text(), nullable=False),
        sa.Column("family", sa.Text(), nullable=False),
        sa.Column("variant", sa.Text(), nullable=False),
        sa.Column("input_payload", PortableJSON, nullable=False),
        sa.Column("world_state_seed", PortableJSON, nullable=False),
        sa.Column("ground_truth", PortableJSON, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_tasks_dataset", "tasks", ["dataset"])
    op.create_index("ix_tasks_family", "tasks", ["family"])

    # 3. submissions table
    op.create_table(
        "submissions",
        sa.Column("submission_id", PortableUUID, primary_key=True),
        sa.Column("team_id", PortableUUID, sa.ForeignKey("teams.team_id", ondelete="CASCADE"), nullable=False),
        sa.Column("attempt_number", sa.Integer(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status", sa.Text(), server_default="in_progress", nullable=False),
        sa.Column("per_task_results", PortableJSON, nullable=True),
        sa.Column("aggregate_score", sa.Numeric(precision=8, scale=4), nullable=True),
        sa.Column("breakdown", PortableJSON, nullable=True),
    )
    op.create_index("ix_submissions_team_id", "submissions", ["team_id"])

    # 4. task_assignments table
    op.create_table(
        "task_assignments",
        sa.Column("id", sa.BigInteger().with_variant(sa.Integer, "sqlite"), primary_key=True, autoincrement=True),
        sa.Column("team_id", PortableUUID, sa.ForeignKey("teams.team_id", ondelete="CASCADE"), nullable=False),
        sa.Column("task_id", sa.Text(), sa.ForeignKey("tasks.task_id", ondelete="CASCADE"), nullable=False),
        sa.Column("assigned_task_id", sa.Text(), nullable=True),
        sa.Column(
            "submission_id",
            PortableUUID,
            sa.ForeignKey("submissions.submission_id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("assigned_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("world_runtime_state", PortableJSON, nullable=False),
    )
    op.create_index("ix_task_assignments_team_id", "task_assignments", ["team_id"])
    op.create_index("ix_task_assignments_task_id", "task_assignments", ["task_id"])
    op.create_index("ix_task_assignments_assigned_task_id", "task_assignments", ["assigned_task_id"])
    op.create_index("ix_task_assignments_submission_id", "task_assignments", ["submission_id"])

    # 5. tool_call_logs table
    op.create_table(
        "tool_call_logs",
        sa.Column("id", sa.BigInteger().with_variant(sa.Integer, "sqlite"), primary_key=True, autoincrement=True),
        sa.Column("team_id", PortableUUID, sa.ForeignKey("teams.team_id", ondelete="CASCADE"), nullable=False),
        sa.Column("task_id", sa.Text(), nullable=True),
        sa.Column("submission_id", PortableUUID, nullable=True),
        sa.Column("tool_name", sa.Text(), nullable=False),
        sa.Column("request_payload", PortableJSON, nullable=True),
        sa.Column("response_payload", PortableJSON, nullable=True),
        sa.Column("was_enforcement_rejection", sa.Boolean(), server_default="false", nullable=False),
        sa.Column("latency_ms", sa.Integer(), server_default="0", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_tool_call_logs_team_id", "tool_call_logs", ["team_id"])
    op.create_index("ix_tool_call_logs_task_id", "tool_call_logs", ["task_id"])
    op.create_index("ix_tool_call_logs_tool_name", "tool_call_logs", ["tool_name"])
    op.create_index("ix_tool_call_logs_created_at", "tool_call_logs", ["created_at"])

    # 6. settings table
    op.create_table(
        "settings",
        sa.Column("key", sa.Text(), primary_key=True),
        sa.Column("value", PortableJSON, nullable=False),
        sa.Column("updated_by", sa.Text(), server_default="system", nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )

    # 7. settings_audit_log table
    op.create_table(
        "settings_audit_log",
        sa.Column("id", sa.BigInteger().with_variant(sa.Integer, "sqlite"), primary_key=True, autoincrement=True),
        sa.Column("key", sa.Text(), nullable=False),
        sa.Column("old_value", PortableJSON, nullable=True),
        sa.Column("new_value", PortableJSON, nullable=False),
        sa.Column("changed_by", sa.Text(), nullable=False),
        sa.Column("changed_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_settings_audit_log_key", "settings_audit_log", ["key"])
    op.create_index("ix_settings_audit_log_changed_at", "settings_audit_log", ["changed_at"])

    # 8. revoked_tokens table
    op.create_table(
        "revoked_tokens",
        sa.Column("id", sa.BigInteger().with_variant(sa.Integer, "sqlite"), primary_key=True, autoincrement=True),
        sa.Column("token_hash", sa.Text(), nullable=False),
        sa.Column("team_id", PortableUUID, sa.ForeignKey("teams.team_id", ondelete="CASCADE"), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_revoked_tokens_token_hash", "revoked_tokens", ["token_hash"], unique=True)
    op.create_index("ix_revoked_tokens_team_id", "revoked_tokens", ["team_id"])


def downgrade() -> None:
    op.drop_table("revoked_tokens")
    op.drop_table("settings_audit_log")
    op.drop_table("settings")
    op.drop_table("tool_call_logs")
    op.drop_table("task_assignments")
    op.drop_table("submissions")
    op.drop_table("tasks")
    op.drop_table("teams")
