import pytest
from sqlalchemy.ext.asyncio import AsyncSession
import sqlalchemy as sa

from agent_arena.models.setting import Setting, SettingsAuditLog
from agent_arena.services.settings_service import SettingsService


@pytest.mark.asyncio
async def test_seed_defaults(db_session: AsyncSession):
    """Verify seeding default settings from settings_defaults.json into PostgreSQL."""
    service = SettingsService(db_session)
    await service.seed_defaults()

    # Verify settings were inserted
    sub_limit = await service.get("submission_limit_per_team")
    assert sub_limit == 5

    phase = await service.get("competition_phase")
    assert phase == "registration"

    weights = await service.get("scoring_weights")
    assert weights["task_success"] == 0.45
    assert weights["policy"] == 0.15

    # Check audit log contains seeded entries
    result = await db_session.execute(sa.select(SettingsAuditLog))
    audit_logs = result.scalars().all()
    assert len(audit_logs) > 0
    assert any(log.key == "submission_limit_per_team" for log in audit_logs)


@pytest.mark.asyncio
async def test_update_setting_and_audit(db_session: AsyncSession):
    """Verify updating a setting creates an audit log entry with old and new values."""
    service = SettingsService(db_session)
    await service.seed_defaults()

    # Update hidden_task_count
    updated_val = await service.set("hidden_task_count", 250, changed_by="lead_organizer")
    assert updated_val == 250

    # Verify updated value in get
    val = await service.get("hidden_task_count")
    assert val == 250

    # Verify audit log
    result = await db_session.execute(
        sa.select(SettingsAuditLog)
        .where(SettingsAuditLog.key == "hidden_task_count")
        .order_by(SettingsAuditLog.id.desc())
    )
    latest_audit = result.scalars().first()
    assert latest_audit is not None
    assert latest_audit.old_value == 200
    assert latest_audit.new_value == 250
    assert latest_audit.changed_by == "lead_organizer"


@pytest.mark.asyncio
async def test_scoring_weights_validation(db_session: AsyncSession):
    """Verify scoring weights must sum to 1.0."""
    service = SettingsService(db_session)
    await service.seed_defaults()

    # Valid custom weights summing to 1.0
    valid_weights = {
        "task_success": 0.50,
        "policy": 0.10,
        "robustness": 0.10,
        "evidence": 0.10,
        "calibration": 0.10,
        "efficiency": 0.05,
        "communication": 0.05,
    }
    updated = await service.set("scoring_weights", valid_weights, changed_by="admin")
    assert updated["task_success"] == 0.50

    # Invalid weights summing to 0.90
    invalid_weights = {
        "task_success": 0.40,
        "policy": 0.10,
        "robustness": 0.10,
        "evidence": 0.10,
        "calibration": 0.10,
        "efficiency": 0.05,
        "communication": 0.05,
    }
    with pytest.raises(ValueError, match="Scoring weights must sum to 1.0"):
        await service.set("scoring_weights", invalid_weights, changed_by="admin")


@pytest.mark.asyncio
async def test_competition_phase_transitions(db_session: AsyncSession):
    """Verify only valid forward phase transitions are permitted."""
    service = SettingsService(db_session)
    await service.seed_defaults()

    # registration -> build (valid forward)
    phase1 = await service.set("competition_phase", "build", changed_by="admin")
    assert phase1 == "build"

    # build -> frozen (valid forward)
    phase2 = await service.set("competition_phase", "frozen", changed_by="admin")
    assert phase2 == "frozen"

    # frozen -> registration (invalid backward transition)
    with pytest.raises(ValueError, match="cannot move backward"):
        await service.set("competition_phase", "registration", changed_by="admin")


@pytest.mark.asyncio
async def test_numeric_bounds_validation(db_session: AsyncSession):
    """Verify numeric settings enforce minimum bounds."""
    service = SettingsService(db_session)
    await service.seed_defaults()

    with pytest.raises(ValueError, match="must be >= 1"):
        await service.set("submission_limit_per_team", 0)

    with pytest.raises(ValueError, match="must be >= 1"):
        await service.set("tool_call_budget_per_task", -5)

    with pytest.raises(ValueError, match="score_aggregation must be one of"):
        await service.set("score_aggregation", "median")
