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
    with pytest.raises(ValueError, match="Live Agent Arena platform only supports the 'hidden' competition dataset"):
        await dataset_service.generate_and_load_dataset(dataset_type="dev")


@pytest.mark.asyncio
async def test_dataset_service_hidden_generation(db_session: AsyncSession):
    """Verify loading static hidden dataset into database using settings count."""
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

    # Verify task attributes
    task = tasks[0]
    assert task.dataset == "hidden"
    assert task.task_id.startswith("TASK-HIDDEN-")
    assert "customer_id" in task.input_payload
    assert "expected_resolution" in task.ground_truth


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
    """Verify that dev tasks and hidden tasks have completely disjoint task IDs and customer IDs."""
    from pathlib import Path
    root = Path(__file__).resolve().parent.parent
    with open(root / "starter-kit" / "mock_simulator" / "data" / "tasks.json", "r", encoding="utf-8") as f:
        dev_tasks = json.load(f)
    with open(root / "src" / "agent_arena" / "data" / "tasks.json", "r", encoding="utf-8") as f:
        hidden_tasks = json.load(f)

    dev_task_ids = {t["task_id"] for t in dev_tasks}
    hidden_task_ids = {t["task_id"] for t in hidden_tasks}
    assert dev_task_ids.isdisjoint(hidden_task_ids)

    dev_cust_ids = {t["input_payload"]["customer_id"] for t in dev_tasks}
    hidden_cust_ids = {t["input_payload"]["customer_id"] for t in hidden_tasks}
    assert dev_cust_ids.isdisjoint(hidden_cust_ids)


@pytest.mark.asyncio
async def test_dataset_service_replace_existing_false_rejects_duplicates(db_session: AsyncSession):
    """Verify that replace_existing=False rejects duplicate tasks and rolls back."""
    settings_service = SettingsService(db_session)
    await settings_service.seed_defaults()

    dataset_service = DatasetService(db_session)
    # Load 12 hidden tasks first
    await dataset_service.generate_and_load_dataset(dataset_type="hidden", count=12, replace_existing=True)

    # Attempt to load again with replace_existing=False
    with pytest.raises(ValueError, match="Duplicate task IDs already exist in database"):
        await dataset_service.generate_and_load_dataset(dataset_type="hidden", count=12, replace_existing=False)

    # Verify original 12 tasks remain in DB intact
    res = await db_session.execute(sa.select(sa.func.count()).select_from(Task).where(Task.dataset == "hidden"))
    assert res.scalar() == 12
