from collections import Counter
from datetime import datetime, timezone
from typing import Any
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from agent_arena.logging import logger
from agent_arena.models.task import Task
from agent_arena.services.settings_service import SettingsService
from agent_arena.tasks.generator import FAMILIES, VARIANTS, TaskGenerator
from agent_arena.tasks.validator import validate_task
from agent_arena.world.generator import generate_world


class DatasetService:
    """Orchestrates deterministic dataset generation, validation, and database loading."""

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
        """Generates, validates, and inserts tasks into the PostgreSQL tasks table.
        
        Pulls task count from settings table if not explicitly passed (PRD §2.2, §8, §12).
        Enforces complete 6x6 family x variant matrix coverage.
        Disjoint seed spaces: dev (1000+) vs hidden (50000+).
        """
        if dataset_type not in {"dev", "hidden"}:
            raise ValueError(f"Invalid dataset_type '{dataset_type}'. Must be 'dev' or 'hidden'.")

        # 1. Pull target count from settings service (never hardcoded)
        if count is None:
            setting_key = "dev_task_count" if dataset_type == "dev" else "hidden_task_count"
            count = await self.settings_service.get(setting_key)
            if count is None:
                count = 70 if dataset_type == "dev" else 200

        # Disjoint seed spaces: dev uses 1000+, hidden uses 50000+
        seed = base_seed if base_seed is not None else (1000 if dataset_type == "dev" else 50000)

        logger.info(f"Generating {count} tasks for '{dataset_type}' dataset using seed {seed}")

        # 2. Generate baseline world
        world = generate_world(seed=seed)

        generator = TaskGenerator(seed=seed)
        generated_tasks: list[dict[str, Any]] = []
        rejected_count = 0
        family_counter: Counter = Counter()
        variant_counter: Counter = Counter()
        matrix: dict[str, dict[str, int]] = {fam: {var: 0 for var in VARIANTS} for fam in FAMILIES}

        # 3. Round-robin generation across families and variants
        idx = 0
        attempt = 0
        max_attempts = count * 3

        while len(generated_tasks) < count and attempt < max_attempts:
            attempt += 1
            family = FAMILIES[idx % len(FAMILIES)]
            variant = VARIANTS[(idx // len(FAMILIES)) % len(VARIANTS)]
            task_num = len(generated_tasks) + 1
            task_id = f"TASK-{dataset_type.upper()}-{task_num:04d}"

            task = generator.generate_task(
                base_world=world,
                family=family,
                variant=variant,
                task_id=task_id,
                dataset=dataset_type,
            )

            # Mandatory validation (PRD §8)
            is_valid, error = validate_task(task)
            if not is_valid:
                logger.warning(f"Task {task_id} rejected by validator: {error}")
                rejected_count += 1
                idx += 1
                continue

            generated_tasks.append(task)
            family_counter[family] += 1
            variant_counter[variant] += 1
            matrix[family][variant] += 1
            idx += 1

        if len(generated_tasks) < count:
            raise RuntimeError(
                f"Failed to generate {count} validated tasks. Generated {len(generated_tasks)}, rejected {rejected_count}."
            )

        # Enforce complete 6x6 matrix coverage if count >= 36
        if count >= len(FAMILIES) * len(VARIANTS):
            for fam in FAMILIES:
                for var in VARIANTS:
                    if matrix[fam][var] == 0:
                        raise RuntimeError(
                            f"6x6 matrix coverage failure: cell ({fam}, {var}) has 0 generated tasks."
                        )

        # 4. Load into PostgreSQL tasks table
        now = datetime.now(timezone.utc)
        loaded_count = 0

        if replace_existing:
            # Clean re-generation: delete previous tasks for this dataset
            await self.session.execute(
                sa.delete(Task).where(Task.dataset == dataset_type)
            )
        else:
            # Check for existing duplicate task_ids to reject cleanly and rollback
            existing_ids = await self.session.execute(
                sa.select(Task.task_id).where(Task.task_id.in_([t["task_id"] for t in generated_tasks]))
            )
            existing = existing_ids.scalars().all()
            if existing:
                await self.session.rollback()
                raise ValueError(
                    f"Duplicate task IDs already exist in database ({len(existing)} found, e.g. '{existing[0]}'). "
                    f"Set replace_existing=True to overwrite."
                )

        try:
            for t in generated_tasks:
                task_model = Task(
                    task_id=t["task_id"],
                    dataset=t["dataset"],
                    family=t["family"],
                    variant=t["variant"],
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
            f"Successfully loaded {loaded_count} validated tasks for '{dataset_type}' dataset.",
            extra={
                "dataset": dataset_type,
                "loaded": loaded_count,
                "rejected": rejected_count,
            },
        )

        return {
            "dataset": dataset_type,
            "target_count": count,
            "generated_count": len(generated_tasks),
            "validated_count": len(generated_tasks),
            "rejected_count": rejected_count,
            "loaded_count": loaded_count,
            "family_distribution": dict(family_counter),
            "variant_distribution": dict(variant_counter),
            "matrix": matrix,
        }
