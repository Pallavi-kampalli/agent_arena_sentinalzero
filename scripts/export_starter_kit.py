import argparse
import json
import sys
from pathlib import Path
from typing import Any

# Ensure src is in python path
ROOT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT_DIR / "src"))

from agent_arena.tasks.generator import FAMILIES, VARIANTS, TaskGenerator  # noqa: E402
from agent_arena.tasks.validator import validate_task  # noqa: E402
from agent_arena.world.generator import generate_world  # noqa: E402


def load_dev_task_count() -> int:
    """Reads the authoritative dev_task_count from settings_defaults.json."""
    defaults_file = ROOT_DIR / "src" / "agent_arena" / "settings_defaults.json"
    if defaults_file.exists():
        with open(defaults_file, "r", encoding="utf-8") as f:
            data = json.load(f)
            return int(data.get("dev_task_count", 70))
    return 70


def generate_dev_tasks(count: int, seed: int = 1000) -> list[dict[str, Any]]:
    """Deterministically generates and validates dev tasks across the 6x6 family x variant matrix."""
    world = generate_world(seed=seed)
    generator = TaskGenerator(seed=seed)
    generated_tasks: list[dict[str, Any]] = []

    idx = 0
    attempt = 0
    max_attempts = count * 3

    while len(generated_tasks) < count and attempt < max_attempts:
        attempt += 1
        family = FAMILIES[idx % len(FAMILIES)]
        variant = VARIANTS[(idx // len(FAMILIES)) % len(VARIANTS)]
        task_num = len(generated_tasks) + 1
        task_id = f"TASK-DEV-{task_num:04d}"

        task = generator.generate_task(
            base_world=world,
            family=family,
            variant=variant,
            task_id=task_id,
            dataset="dev",
        )

        is_valid, error = validate_task(task)
        if not is_valid:
            idx += 1
            continue

        # Sanitize task representation for public starter-kit (dev dataset)
        public_task = {
            "task_id": task["task_id"],
            "dataset": "dev",
            "family": task["family"],
            "variant": task["variant"],
            "input_payload": task["input_payload"],
            "world_state_seed": task["world_state_seed"],
            "ground_truth": task["ground_truth"],
        }
        generated_tasks.append(public_task)
        idx += 1

    if len(generated_tasks) < count:
        raise RuntimeError(f"Failed to generate {count} validated dev tasks (got {len(generated_tasks)}).")

    # Validate 6x6 family x variant coverage
    found_families = {t["family"] for t in generated_tasks}
    found_variants = {t["variant"] for t in generated_tasks}
    if len(found_families) < len(FAMILIES) or len(found_variants) < len(VARIANTS):
        raise RuntimeError(
            f"Incomplete matrix coverage: families={len(found_families)}/{len(FAMILIES)}, "
            f"variants={len(found_variants)}/{len(VARIANTS)}"
        )

    return generated_tasks


def extract_domain_rules_code() -> str:
    """Extracts canonical domain logic from src/agent_arena/domain/rules.py and models.py."""
    rules_path = ROOT_DIR / "src" / "agent_arena" / "domain" / "rules.py"
    with open(rules_path, "r", encoding="utf-8") as f:
        rules_code = f.read()

    # Read models.py for EligibilityResult
    models_path = ROOT_DIR / "src" / "agent_arena" / "domain" / "models.py"
    with open(models_path, "r", encoding="utf-8") as f:
        _models_code = f.read()

    # Extract EligibilityResult definition
    eligibility_result_block = """@dataclass
class EligibilityResult:
    is_eligible: bool
    status: str = "success"
    reason: str | None = None
    policy_ref: str | None = None
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        if self.is_eligible:
            return {"status": self.status}
        res = {"error": self.error or "INELIGIBLE"}
        if self.reason:
            res["reason"] = self.reason
        if self.policy_ref:
            res["policy_ref"] = self.policy_ref
        return res
"""

    # Remove `from agent_arena.domain.models import EligibilityResult` from rules_code
    cleaned_rules = rules_code.replace("from agent_arena.domain.models import EligibilityResult", "")
    # Remove top imports since server.py imports them
    lines = cleaned_rules.splitlines()
    body_lines = [
        line
        for line in lines
        if not line.startswith("import copy")
        and not line.startswith("from datetime import")
        and not line.startswith("from decimal import")
        and not line.startswith("from typing import")
    ]
    cleaned_body = "\n".join(body_lines).strip()

    generated_block = f"""# =============================================================================
# GENERATED FROM src/agent_arena/domain/rules.py & models.py
# DO NOT EDIT MANUALLY - Run `python scripts/export_starter_kit.py` to regenerate
# =============================================================================


{eligibility_result_block}

{cleaned_body}


# =============================================================================
# END GENERATED DOMAIN SECTION
# ============================================================================="""
    return generated_block


def main() -> None:
    parser = argparse.ArgumentParser(description="Export public dev dataset and sync canonical rules for starter kit")
    parser.add_argument("--check", action="store_true", help="Check for staleness without writing files (CI guard)")
    parser.add_argument("--count", type=int, default=None, help="Override dev task count (default: from settings)")
    args = parser.parse_args()

    count = args.count if args.count is not None else load_dev_task_count()
    print(f"Authoritative dev_task_count: {count}")

    starter_kit_dir = ROOT_DIR / "starter-kit"
    mock_sim_dir = starter_kit_dir / "mock_simulator"
    mock_sim_dir.mkdir(parents=True, exist_ok=True)

    tasks_file = mock_sim_dir / "dev_tasks.json"
    server_file = mock_sim_dir / "server.py"

    # 1. Generate dev tasks JSON
    tasks = generate_dev_tasks(count=count, seed=1000)
    tasks_json = json.dumps({"schema_version": 1, "task_count": count, "tasks": tasks}, indent=2)

    # 2. Check or write dev_tasks.json
    if args.check:
        if not tasks_file.exists():
            print(f"ERROR: {tasks_file} does not exist.")
            sys.exit(1)
        with open(tasks_file, "r", encoding="utf-8") as f:
            existing_tasks_json = f.read()
        if json.loads(existing_tasks_json) != json.loads(tasks_json):
            print(f"ERROR: {tasks_file} is stale relative to generator settings.")
            sys.exit(1)
        print(f"PASS: {tasks_file} is up to date.")
    else:
        with open(tasks_file, "w", encoding="utf-8") as f:
            f.write(tasks_json)
        print(f"Wrote {len(tasks)} tasks to {tasks_file}")

    # 3. Check or sync generated domain section in server.py
    domain_block = extract_domain_rules_code()
    if server_file.exists():
        with open(server_file, "r", encoding="utf-8") as f:
            server_content = f.read()

        start_marker = "# =============================================================================\n# GENERATED FROM src/agent_arena/domain/rules.py & models.py"
        end_marker = "# END GENERATED DOMAIN SECTION\n# ============================================================================="

        if start_marker in server_content and end_marker in server_content:
            before = server_content[: server_content.find(start_marker)]
            after = server_content[server_content.find(end_marker) + len(end_marker) :]
            new_server_content = before + domain_block + after

            if args.check:
                if server_content != new_server_content:
                    print(f"ERROR: {server_file} domain section is stale relative to src/agent_arena/domain/rules.py.")
                    sys.exit(1)
                print(f"PASS: {server_file} domain rules are up to date.")
            else:
                with open(server_file, "w", encoding="utf-8") as f:
                    f.write(new_server_content)
                print(f"Synchronized canonical domain rules in {server_file}")
        else:
            if args.check:
                print(f"ERROR: {server_file} missing generated domain section markers.")
                sys.exit(1)
    else:
        if args.check:
            print(f"ERROR: {server_file} does not exist.")
            sys.exit(1)


if __name__ == "__main__":
    main()
