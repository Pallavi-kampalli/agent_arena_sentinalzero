import argparse
import json
import sys
from pathlib import Path
from typing import Any

# Ensure src is in python path
ROOT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT_DIR / "src"))


def load_dev_task_count() -> int:
    """Returns the canonical dev_task_count for the mock simulator (30 tasks)."""
    return 30


def load_canonical_dev_tasks(mock_data_dir: Path) -> list[dict[str, Any]]:
    """Loads canonical dev tasks from mock_data/."""
    sys.path.insert(0, str(mock_data_dir))
    from sync_mock_data import build_dev_tasks
    return build_dev_tasks(mock_data_dir)


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

    data_dir = mock_sim_dir / "data"
    server_file = mock_sim_dir / "server.py"

    required_files = [
        "tasks.json",
        "ground_truth.json",
        "customers.json",
        "transactions.json",
        "subscriptions.json",
        "policies.json",
        "previous_cases.json",
    ]

    for fname in required_files:
        if not (data_dir / fname).exists():
            raise FileNotFoundError(f"Missing required mock data file: {data_dir / fname}")

    # 2. Check or sync generated domain section in server.py
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

    # 3. Mirror copy of starter kit to participant directory (export only, no tests)
    participant_dir = ROOT_DIR.parent / "agent_arena_participant"
    if participant_dir.exists() and not args.check:
        import shutil
        shutil.copytree(
            starter_kit_dir,
            participant_dir,
            dirs_exist_ok=True,
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.db", ".venv", ".git", ".pytest_cache", ".env"),
        )
        print(f"Synchronized starter-kit copy to {participant_dir}")


if __name__ == "__main__":
    main()
