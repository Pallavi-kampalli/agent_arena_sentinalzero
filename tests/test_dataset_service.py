import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from agent_arena.models.task import Task
from agent_arena.services.dataset_service import DatasetService
from agent_arena.services.settings_service import SettingsService
from agent_arena.world.generator import generate_world



@pytest.mark.asyncio
async def test_dataset_service_dev_generation(db_session: AsyncSession):
    """Verify generating and loading dev dataset into database using settings count."""
    settings_service = SettingsService(db_session)
    await settings_service.seed_defaults()

    # Set smaller count for fast unit test
    await settings_service.set("dev_task_count", 12)

    dataset_service = DatasetService(db_session)
    result = await dataset_service.generate_and_load_dataset(dataset_type="dev")

    assert result["dataset"] == "dev"
    assert result["loaded_count"] == 12
    assert result["validated_count"] == 12
    assert result["rejected_count"] == 0

    # Query tasks table in database
    db_res = await db_session.execute(sa.select(Task).where(Task.dataset == "dev"))
    tasks = db_res.scalars().all()
    assert len(tasks) == 12

    # Verify task attributes
    task = tasks[0]
    assert task.dataset == "dev"
    assert task.task_id.startswith("TASK-DEV-")
    assert "customer_id" in task.input_payload
    assert "expected_resolution" in task.ground_truth


@pytest.mark.asyncio
async def test_dataset_service_hidden_generation(db_session: AsyncSession):
    """Verify generating and loading hidden dataset into database using settings count."""
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

    # Verify all 6 families are represented
    families = {t.family for t in tasks}
    assert len(families) == 6


@pytest.mark.asyncio
async def test_dataset_service_uses_updated_setting(db_session: AsyncSession):
    """Verify changing dev_task_count in settings immediately changes generation count without code change."""
    settings_service = SettingsService(db_session)
    await settings_service.seed_defaults()

    await settings_service.set("dev_task_count", 8)

    dataset_service = DatasetService(db_session)
    result = await dataset_service.generate_and_load_dataset(dataset_type="dev")
    assert result["loaded_count"] == 8

    # Update setting to 15
    await settings_service.set("dev_task_count", 15)
    result2 = await dataset_service.generate_and_load_dataset(dataset_type="dev")
    assert result2["loaded_count"] == 15


@pytest.mark.asyncio
async def test_dataset_service_6x6_matrix_coverage(db_session: AsyncSession):
    """Verify that generating 70 dev tasks covers all 36 cells of the 6x6 matrix."""
    settings_service = SettingsService(db_session)
    await settings_service.seed_defaults()

    dataset_service = DatasetService(db_session)
    result = await dataset_service.generate_and_load_dataset(dataset_type="dev", count=70)
    matrix = result["matrix"]
    assert len(matrix) == 6
    for fam, variants in matrix.items():
        assert len(variants) == 6
        for var, cnt in variants.items():
            assert cnt > 0, f"Cell ({fam}, {var}) has 0 tasks!"


def test_dataset_service_seed_disjointness():
    """Verify that dev world (seed 1000) and hidden world (seed 50000) have disjoint entities."""
    dev_world = generate_world(seed=1000)
    hidden_world = generate_world(seed=50000)

    dev_cust_ids = {c["id"] for c in dev_world["customers"]}
    hidden_cust_ids = {c["id"] for c in hidden_world["customers"]}
    assert dev_cust_ids.isdisjoint(hidden_cust_ids)

    dev_tx_ids = {t["id"] for t in dev_world["transactions"]}
    hidden_tx_ids = {t["id"] for t in hidden_world["transactions"]}
    assert dev_tx_ids.isdisjoint(hidden_tx_ids)


@pytest.mark.asyncio
async def test_dataset_service_replace_existing_false_rejects_duplicates(db_session: AsyncSession):
    """Verify that replace_existing=False rejects duplicate tasks and rolls back."""
    settings_service = SettingsService(db_session)
    await settings_service.seed_defaults()

    dataset_service = DatasetService(db_session)
    # Load 12 dev tasks first
    await dataset_service.generate_and_load_dataset(dataset_type="dev", count=12, replace_existing=True)

    # Attempt to load again with replace_existing=False
    with pytest.raises(ValueError, match="Duplicate task IDs already exist in database"):
        await dataset_service.generate_and_load_dataset(dataset_type="dev", count=12, replace_existing=False)

    # Verify original 12 tasks remain in DB intact
    res = await db_session.execute(sa.select(sa.func.count()).select_from(Task).where(Task.dataset == "dev"))
    assert res.scalar() == 12

