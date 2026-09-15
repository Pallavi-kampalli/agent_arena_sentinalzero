import uuid
from datetime import datetime, timezone
import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from agent_arena.models import (
    Base,
    Team,
    Task,
    Submission,
    TaskAssignment,
    ToolCallLog,
    Setting,
    SettingsAuditLog,
)


@pytest.mark.asyncio
async def test_tables_created(test_engine):
    """Verify all 7 tables exist in the database schema."""
    async with test_engine.connect() as conn:
        tables = await conn.run_sync(
            lambda sync_conn: sa.inspect(sync_conn).get_table_names()
        )
    expected_tables = {
        "teams",
        "tasks",
        "submissions",
        "task_assignments",
        "tool_call_logs",
        "settings",
        "settings_audit_log",
    }
    assert expected_tables.issubset(set(tables)), f"Missing tables: {expected_tables - set(tables)}"


@pytest.mark.asyncio
async def test_team_crud(db_session: AsyncSession):
    """Verify Team model operations."""
    team_id = uuid.uuid4()
    team = Team(
        team_id=team_id,
        team_name="Alpha Team",
        members=[{"name": "Alice", "email": "alice@example.com"}],
        github_repo_url="https://github.com/alpha/repo",
        bearer_token_hash="fake_hash_value_123",
        token_version=1,
        status="active",
    )
    db_session.add(team)
    await db_session.commit()

    loaded = await db_session.get(Team, team_id)
    assert loaded is not None
    assert loaded.team_name == "Alpha Team"
    assert loaded.status == "active"
    assert loaded.token_version == 1
    assert loaded.members[0]["name"] == "Alice"


@pytest.mark.asyncio
async def test_task_and_assignments(db_session: AsyncSession):
    """Verify Task, Submission, and TaskAssignment relationships."""
    team = Team(
        team_id=uuid.uuid4(),
        team_name="Beta Team",
        bearer_token_hash="hash_beta",
        token_version=1,
        status="active",
    )
    db_session.add(team)

    task = Task(
        task_id="TASK-001",
        dataset="hidden",
        family="duplicate_payment",
        variant="adversarial",
        input_payload={"customer_message": "Double charged", "customer_id": "CUS-100"},
        world_state_seed={"customers": [{"id": "CUS-100"}]},
        ground_truth={"expected_end_state": {"refund": True}, "required_evidence": ["TXN-1"]},
    )
    db_session.add(task)

    submission = Submission(
        submission_id=uuid.uuid4(),
        team_id=team.team_id,
        attempt_number=1,
        status="in_progress",
    )
    db_session.add(submission)
    await db_session.flush()

    assignment = TaskAssignment(
        team_id=team.team_id,
        task_id=task.task_id,
        submission_id=submission.submission_id,
        world_runtime_state={"customers": [{"id": "CUS-100"}]},
    )
    db_session.add(assignment)

    tool_log = ToolCallLog(
        team_id=team.team_id,
        task_id=task.task_id,
        submission_id=submission.submission_id,
        tool_name="get_customer",
        request_payload={"customer_id": "CUS-100"},
        response_payload={"id": "CUS-100", "tier": "gold"},
        was_enforcement_rejection=False,
        latency_ms=45,
    )
    db_session.add(tool_log)
    await db_session.commit()

    loaded_assignment = await db_session.get(TaskAssignment, assignment.id)
    assert loaded_assignment is not None
    assert loaded_assignment.team_id == team.team_id
    assert loaded_assignment.task_id == "TASK-001"

    loaded_log = await db_session.get(ToolCallLog, tool_log.id)
    assert loaded_log is not None
    assert loaded_log.tool_name == "get_customer"
    assert loaded_log.latency_ms == 45


@pytest.mark.asyncio
async def test_setting_and_audit_log(db_session: AsyncSession):
    """Verify Setting and SettingsAuditLog models."""
    setting = Setting(
        key="test_key",
        value={"foo": "bar"},
        updated_by="admin_user",
    )
    db_session.add(setting)

    audit = SettingsAuditLog(
        key="test_key",
        old_value=None,
        new_value={"foo": "bar"},
        changed_by="admin_user",
    )
    db_session.add(audit)
    await db_session.commit()

    loaded_setting = await db_session.get(Setting, "test_key")
    assert loaded_setting is not None
    assert loaded_setting.value == {"foo": "bar"}

    loaded_audit = await db_session.get(SettingsAuditLog, audit.id)
    assert loaded_audit is not None
    assert loaded_audit.changed_by == "admin_user"
    assert loaded_audit.new_value == {"foo": "bar"}
