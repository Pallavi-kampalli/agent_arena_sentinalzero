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
DEV_DATA_DIR = Path(__file__).resolve().parent.parent.parent.parent / "starter-kit" / "mock_simulator" / "data"


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


def load_canonical_tasks(data_dir: Path, dataset_type: str | None = None) -> list[dict[str, Any]]:
    """Loads modular tasks, ground truth, and base entity catalogs from data_dir or mock simulator."""
    files_to_load: list[tuple[Path, str, str]] = []
    if dataset_type == "dev":
        source_dir = data_dir if (data_dir / "tasks.json").exists() else DEV_DATA_DIR
        files_to_load = [(source_dir, "tasks.json", "ground_truth.json")]
    elif dataset_type == "hidden":
        files_to_load = [(data_dir, "tasks_hidden.json", "ground_truth_hidden.json")]
    else:
        source_dev = data_dir if (data_dir / "tasks.json").exists() else DEV_DATA_DIR
        if (source_dev / "tasks.json").exists():
            files_to_load.append((source_dev, "tasks.json", "ground_truth.json"))
        if (data_dir / "tasks_hidden.json").exists():
            files_to_load.append((data_dir, "tasks_hidden.json", "ground_truth_hidden.json"))

    if not files_to_load:
        raise FileNotFoundError(f"No task files found in {data_dir}")

    tasks: list[dict[str, Any]] = []
    gt_map: dict[str, dict[str, Any]] = {}

    for folder, t_fname, gt_fname in files_to_load:
        t_path = folder / t_fname
        gt_path = folder / gt_fname
        if t_path.exists():
            with open(t_path, "r", encoding="utf-8") as f:
                tasks.extend(json.load(f))
        if gt_path.exists():
            with open(gt_path, "r", encoding="utf-8") as f:
                gts = json.load(f)
                for g in gts:
                    gt_map[g["task_id"]] = g

    def _read_json(fname: str) -> list[dict[str, Any]]:
        p = data_dir / fname
        if p.exists():
            with open(p, "r", encoding="utf-8") as f:
                return json.load(f)
        return []

    directory = _read_json("directory.json")
    domains = _read_json("domains.json")
    threat_intel = _read_json("threat_intel.json")
    security_policies = _read_json("security_policies.json")
    if not security_policies:
        security_policies = _read_json("policies.json")
    historical_threats = _read_json("historical_threats.json")

    compiled_tasks = []
    for t in tasks:
        tid = t["task_id"]
        gt = gt_map.get(tid, {})
        inp = t.get("input_payload", {})
        overrides = t.get("task_overrides", {})

        world = {
            "seed": 50000,
            "current_date": "2026-09-15T00:00:00+00:00",
            "directory": copy.deepcopy(directory),
            "domains": copy.deepcopy(domains),
            "threat_intel": copy.deepcopy(threat_intel),
            "security_policies": copy.deepcopy(security_policies),
            "policies": copy.deepcopy(security_policies),
            "historical_threats": copy.deepcopy(historical_threats),
            "threads": copy.deepcopy(overrides.get("threads", [])),
            "actions_taken": [],
            "target_message_id": inp.get("message_id", ""),
            "target_thread_id": inp.get("thread_id", ""),
        }

        if "directory" in overrides:
            world["directory"] = merge_entities_by_key(world["directory"], overrides["directory"], "id")
        if "domains" in overrides:
            world["domains"] = merge_entities_by_key(world["domains"], overrides["domains"], "domain_id")
        if "threat_intel" in overrides:
            world["threat_intel"] = merge_entities_by_key(world["threat_intel"], overrides["threat_intel"], "domain_id")
        if "security_policies" in overrides:
            world["security_policies"] = merge_entities_by_key(world["security_policies"], overrides["security_policies"], "id")
            world["policies"] = copy.deepcopy(world["security_policies"])

        compiled_tasks.append({
            "task_id": tid,
            "dataset": t.get("dataset", "dev"),
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
        """Loads canonical pre-generated static tasks into the PostgreSQL tasks table."""
        if dataset_type == "dev":
            raise ValueError("Loading 'dev' dataset into live platform is disallowed. Only 'hidden' dataset is supported.")

        raw_tasks = load_canonical_tasks(DATA_DIR, dataset_type)
        if not raw_tasks:
            raise FileNotFoundError(f"No task files found in {DATA_DIR}")

        # Filter by dataset type if specified
        matching_tasks = [t for t in raw_tasks if t.get("dataset") == dataset_type]
        if not matching_tasks:
            matching_tasks = raw_tasks

        if count is None:
            setting_key = f"{dataset_type}_task_count"
            count = await self.settings_service.get(setting_key)
            if count is None:
                count = len(matching_tasks)

        tasks_to_load = matching_tasks[:count] if count is not None else matching_tasks

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
                gt = t.get("ground_truth", {})
                classification = gt.get("classification", {}) if isinstance(gt, dict) else {}
                family = t.get("family") or classification.get("category") or "cybersecurity_triage"
                variant = t.get("variant") or classification.get("issue") or "standard"

                task_model = Task(
                    task_id=t["task_id"],
                    dataset=t.get("dataset", dataset_type),
                    family=family,
                    variant=variant,
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
