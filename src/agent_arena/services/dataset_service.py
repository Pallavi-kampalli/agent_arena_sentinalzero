import copy
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from agent_arena.logging import logger
from agent_arena.models.task import Task
from agent_arena.services.settings_service import SettingsService

DATA_DIR = Path(__file__).resolve().parent.parent / "data"


def merge_entities_by_key(
    base: list[dict[str, Any]],
    overrides: list[dict[str, Any]],
    key: str,
) -> list[dict[str, Any]]:
    """Merges list of dict entities by primary key, with overrides replacing base items."""
    merged = {item[key]: copy.deepcopy(item) for item in base if isinstance(item, dict) and key in item}
    for item in overrides:
        if isinstance(item, dict) and key in item:
            merged[item[key]] = copy.deepcopy(item)
    return list(merged.values())


def load_canonical_tasks(data_dir: Path) -> list[dict[str, Any]]:
    """Loads modular tasks, ground truth, and base entity catalogs from data_dir."""
    tasks_file = data_dir / "tasks.json"
    gt_file = data_dir / "ground_truth.json"

    if not tasks_file.exists():
        raise FileNotFoundError(f"tasks.json not found in {data_dir}")

    with open(tasks_file, "r", encoding="utf-8") as f:
        tasks = json.load(f)

    gt_map = {}
    if gt_file.exists():
        with open(gt_file, "r", encoding="utf-8") as f:
            gts = json.load(f)
            gt_map = {g["task_id"]: g for g in gts}

    def _read_json(fname: str) -> list[dict[str, Any]]:
        p = data_dir / fname
        if p.exists():
            with open(p, "r", encoding="utf-8") as f:
                return json.load(f)
        return []

    customers = _read_json("customers.json")
    transactions = _read_json("transactions.json")
    subscriptions = _read_json("subscriptions.json")
    policies = _read_json("policies.json")
    previous_cases = _read_json("previous_cases.json")

    compiled_tasks = []
    for t in tasks:
        tid = t["task_id"]
        gt = gt_map.get(tid, {})
        inp = t.get("input_payload", {})
        overrides = t.get("task_overrides", {})

        world = {
            "seed": 50000,
            "current_date": "2026-09-15T00:00:00+00:00",
            "customers": copy.deepcopy(customers),
            "transactions": copy.deepcopy(transactions),
            "subscriptions": copy.deepcopy(subscriptions),
            "policies": copy.deepcopy(policies),
            "documents": copy.deepcopy(policies),
            "historical_cases": copy.deepcopy(previous_cases),
            "previous_cases": copy.deepcopy(previous_cases),
            "verification_requests": [],
            "escalations": [],
            "target_customer_id": inp.get("customer_id", ""),
        }

        if "customers" in overrides:
            world["customers"] = merge_entities_by_key(world["customers"], overrides["customers"], "id")
        if "transactions" in overrides:
            world["transactions"] = merge_entities_by_key(world["transactions"], overrides["transactions"], "id")
        if "subscriptions" in overrides:
            world["subscriptions"] = merge_entities_by_key(world["subscriptions"], overrides["subscriptions"], "id")
        if "policies" in overrides:
            world["policies"] = merge_entities_by_key(world["policies"], overrides["policies"], "id")
            world["documents"] = merge_entities_by_key(world["documents"], overrides["policies"], "id")
        if "previous_cases" in overrides:
            world["previous_cases"] = merge_entities_by_key(world["previous_cases"], overrides["previous_cases"], "case_id")
            world["historical_cases"] = merge_entities_by_key(world["historical_cases"], overrides["previous_cases"], "case_id")

        compiled_tasks.append({
            "task_id": tid,
            "dataset": t.get("dataset", "hidden"),
            "input_payload": inp,
            "world_state_seed": world,
            "ground_truth": gt,
        })

    return compiled_tasks


class DatasetService:
    """Orchestrates loading of canonical static datasets into the database."""

    def __init__(self, session: AsyncSession):
        self.session = session
        self.settings_service = SettingsService(session)

    async def generate_and_load_dataset(
        self,
        dataset_type: str,  # 'dev' | 'hidden'
        count: int | None = None,
        base_seed: int | None = None,
        replace_existing: bool = True,
    ) -> dict[str, Any]:
        """Loads canonical pre-generated static tasks into the PostgreSQL tasks table.

        Zero runtime seed generation: every team is evaluated on the exact same
        canonical fixed tasks and world state.
        """
        if dataset_type != "hidden":
            raise ValueError(
                f"Live Agent Arena platform only supports the 'hidden' competition dataset (got '{dataset_type}'). "
                f"Development tasks are isolated to the mock simulator."
            )

        raw_tasks = load_canonical_tasks(DATA_DIR)
        if not raw_tasks:
            raise FileNotFoundError(f"No task files found in {DATA_DIR}")

        # Pull target count from settings service if not passed
        if count is None:
            setting_key = "hidden_task_count"
            count = await self.settings_service.get(setting_key)
            if count is None:
                count = len(raw_tasks)

        tasks_to_load = raw_tasks[:count] if count is not None else raw_tasks

        # Load into PostgreSQL tasks table
        now = datetime.now(UTC)
        loaded_count = 0

        if replace_existing:
            await self.session.execute(sa.delete(Task).where(Task.dataset == dataset_type))
        else:
            existing_ids = await self.session.execute(
                sa.select(Task.task_id).where(Task.task_id.in_([t["task_id"] for t in tasks_to_load]))
            )
            existing = existing_ids.scalars().all()
            if existing:
                await self.session.rollback()
                raise ValueError(
                    f"Duplicate task IDs already exist in database ({len(existing)} found, e.g. '{existing[0]}'). "
                    f"Set replace_existing=True to overwrite."
                )

        try:
            for t in tasks_to_load:
                task_model = Task(
                    task_id=t["task_id"],
                    dataset=t.get("dataset", dataset_type),
                    family=None,
                    variant=None,
                    input_payload=t["input_payload"],
                    world_state_seed=t["world_state_seed"],
                    ground_truth=t["ground_truth"],
                    created_at=now,
                )
                self.session.add(task_model)
                loaded_count += 1

            await self.session.commit()
        except Exception:
            await self.session.rollback()
            raise

        logger.info(
            f"Successfully loaded {loaded_count} canonical static tasks for '{dataset_type}' dataset.",
            extra={
                "dataset": dataset_type,
                "loaded": loaded_count,
            },
        )

        return {
            "dataset": dataset_type,
            "target_count": count,
            "generated_count": len(tasks_to_load),
            "validated_count": len(tasks_to_load),
            "rejected_count": 0,
            "loaded_count": loaded_count,
        }
