"""DatasetService tests for SentinelZero hidden dataset.

Verifies that:
- 'dev' dataset is rejected on the live platform
- Hidden dataset loads with correct SentinelZero task format
- Dev tasks (mock simulator) and hidden tasks have disjoint task IDs
- Duplicate task loading is correctly rejected
"""

import json

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from agent_arena.models.task import Task
from agent_arena.services.dataset_service import DatasetService
from agent_arena.services.settings_service import SettingsService


@pytest.mark.asyncio
async def test_dataset_service_dev_rejected(db_session: AsyncSession):
    """Verify that attempting to load 'dev' dataset in live platform is rejected."""
    dataset_service = DatasetService(db_session)
    with pytest.raises((ValueError, Exception)):
        await dataset_service.generate_and_load_dataset(dataset_type="dev")


@pytest.mark.asyncio
async def test_dataset_service_hidden_generation(db_session: AsyncSession):
    """Verify loading static hidden SentinelZero dataset into database."""
    settings_service = SettingsService(db_session)
    await settings_service.seed_defaults()

    await settings_service.set("hidden_task_count", 18)

    dataset_service = DatasetService(db_session)
    result = await dataset_service.generate_and_load_dataset(dataset_type="hidden")

    assert result["dataset"] == "hidden"
    assert result["loaded_count"] == 18

    # Query tasks table
    db_res = await db_session.execute(sa.select(Task).where(Task.dataset == "hidden"))
    tasks = db_res.scalars().all()
    assert len(tasks) == 18

    # Verify SentinelZero task attributes
    task = tasks[0]
    assert task.dataset == "hidden"
    assert task.task_id.startswith("TASK-HIDDEN-")
    # SentinelZero uses message_id, sender, recipient in input_payload (not customer_id)
    assert "message_id" in task.input_payload or "sender" in task.input_payload
    # Ground truth uses expected_resolution with SZ decision types: allow/warn/quarantine/escalate
    assert "expected_resolution" in task.ground_truth
    assert task.ground_truth["expected_resolution"] in ("allow", "warn", "quarantine", "escalate")


@pytest.mark.asyncio
async def test_dataset_service_uses_updated_setting(db_session: AsyncSession):
    """Verify changing hidden_task_count in settings immediately changes loaded count."""
    settings_service = SettingsService(db_session)
    await settings_service.seed_defaults()

    await settings_service.set("hidden_task_count", 8)

    dataset_service = DatasetService(db_session)
    result = await dataset_service.generate_and_load_dataset(dataset_type="hidden")
    assert result["loaded_count"] == 8

    # Update setting to 15
    await settings_service.set("hidden_task_count", 15)
    result2 = await dataset_service.generate_and_load_dataset(dataset_type="hidden")
    assert result2["loaded_count"] == 15


def test_dataset_service_task_disjointness():
    """Verify dev tasks and hidden tasks have completely disjoint task IDs."""
    from pathlib import Path
    root = Path(__file__).resolve().parent.parent
    with open(root / "starter-kit" / "mock_simulator" / "data" / "tasks.json", "r", encoding="utf-8") as f:
        dev_tasks = json.load(f)
    with open(root / "src" / "agent_arena" / "data" / "tasks.json", "r", encoding="utf-8") as f:
        hidden_tasks = json.load(f)

    dev_task_ids = {t["task_id"] for t in dev_tasks}
    hidden_task_ids = {t["task_id"] for t in hidden_tasks}
    assert dev_task_ids.isdisjoint(hidden_task_ids), (
        f"Dev and hidden tasks share task IDs: {dev_task_ids & hidden_task_ids}"
    )

    # For SentinelZero: check message_id disjointness (SZ uses message_id, SupportOps uses customer_id)
    def get_primary_id(task):
        inp = task.get("input_payload", {})
        return inp.get("message_id") or inp.get("customer_id") or ""

    dev_primary_ids = {get_primary_id(t) for t in dev_tasks}
    hidden_primary_ids = {get_primary_id(t) for t in hidden_tasks}
    # Remove empty strings from comparison
    dev_primary_ids.discard("")
    hidden_primary_ids.discard("")
    assert dev_primary_ids.isdisjoint(hidden_primary_ids), (
        f"Dev and hidden tasks share primary entity IDs: {dev_primary_ids & hidden_primary_ids}"
    )


@pytest.mark.asyncio
async def test_dataset_service_replace_existing_false_rejects_duplicates(db_session: AsyncSession):
    """Verify that replace_existing=False rejects duplicate tasks and rolls back."""
    settings_service = SettingsService(db_session)
    await settings_service.seed_defaults()

    dataset_service = DatasetService(db_session)
    # Load 12 hidden tasks first
    await dataset_service.generate_and_load_dataset(dataset_type="hidden", count=12, replace_existing=True)

    # Attempt to load again with replace_existing=False
    with pytest.raises((ValueError, Exception)):
        await dataset_service.generate_and_load_dataset(dataset_type="hidden", count=12, replace_existing=False)

    # Verify original 12 tasks remain in DB intact
    res = await db_session.execute(sa.select(sa.func.count()).select_from(Task).where(Task.dataset == "hidden"))
    assert res.scalar() == 12
