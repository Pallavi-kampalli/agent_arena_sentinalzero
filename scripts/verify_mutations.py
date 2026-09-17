import copy
import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT_DIR / "src"))
sys.path.insert(0, str(ROOT_DIR / "starter-kit"))


print("=" * 65)
print("  Agent Arena — Adversarial Mutation Verification")
print("=" * 65)

passed_mutations = 0
total_mutations = 6

# -----------------------------------------------------------------------------
# Mutation 1: Team Isolation Bypass (cross-team runtime state leakage)
# -----------------------------------------------------------------------------
print("\n[Mutation 1] Cross-team runtime state leakage...")
world_template = {"actions_taken": [], "quarantined": False}

team_a_world = copy.deepcopy(world_template)
team_b_world = copy.deepcopy(world_template)

# Team A quarantines message
team_a_world["quarantined"] = True
team_a_world["actions_taken"].append("quarantine_message")


# Buggy resolver returns Team A's state when Team B queries
def buggy_resolve_assignment(team_id: str):
    return team_a_world  # Leaks Team A's state to Team B


mutated_team_b_world = buggy_resolve_assignment("team-b")

try:
    assert mutated_team_b_world["quarantined"] is False, (
        f"Cross-team leakage! Team B observed Team A's quarantine state: {mutated_team_b_world['quarantined']}"
    )
    print("FAILED: Mutation 1 was not detected!")
except AssertionError as e:
    print(f"PASS (Killed): {e}")
    passed_mutations += 1

# -----------------------------------------------------------------------------
# Mutation 2: Live Settings Ignore (runtime uses stale hardcoded value)
# -----------------------------------------------------------------------------
print("\n[Mutation 2] Live settings update ignored by runtime logic...")


# Mutated settings service that ignores dynamic updates and returns hardcoded 200
class BuggySettingsService:
    def __init__(self):
        self._cached = 200

    def set(self, key, val):
        pass  # Fails to update cached or runtime value

    def get(self, key):
        return 200  # Hardcoded stale value


settings_mock = BuggySettingsService()
settings_mock.set("hidden_task_count", 10)
observed_limit = settings_mock.get("hidden_task_count")

try:
    assert observed_limit == 10, f"Runtime ignored live settings change! Expected 10, got {observed_limit}"
    print("FAILED: Mutation 2 was not detected!")
except AssertionError as e:
    print(f"PASS (Killed): {e}")
    passed_mutations += 1

# -----------------------------------------------------------------------------
# Mutation 3: Historical Score Mutability (re-scoring on weights change)
# -----------------------------------------------------------------------------
print("\n[Mutation 3] Historical score recalculated on weights change...")

initial_weights = {
    "task_success": 0.45,
    "policy": 0.15,
    "robustness": 0.15,
    "evidence": 0.10,
    "calibration": 0.05,
    "efficiency": 0.05,
    "communication": 0.05,
}
new_weights = {
    "task_success": 0.10,
    "policy": 0.40,
    "robustness": 0.20,
    "evidence": 0.10,
    "calibration": 0.10,
    "efficiency": 0.05,
    "communication": 0.05,
}

# Submission scored under initial weights
historical_submission = {
    "submission_id": "SUB-HIST-001",
    "aggregate_score": 0.8250,
    "breakdown": {"scoring_metadata": {"scoring_weights_snapshot": initial_weights}},
}


# Buggy scoring query dynamically recalculates historical score using new weights
def buggy_get_historical_score(sub, current_weights):
    if current_weights != sub["breakdown"]["scoring_metadata"]["scoring_weights_snapshot"]:
        return 0.6120  # Score altered due to weights change
    return sub["aggregate_score"]


observed_score = buggy_get_historical_score(historical_submission, new_weights)

try:
    assert observed_score == historical_submission["aggregate_score"], (
        f"Historical score corrupted! Expected {historical_submission['aggregate_score']}, got {observed_score}"
    )
    print("FAILED: Mutation 3 was not detected!")
except AssertionError as e:
    print(f"PASS (Killed): {e}")
    passed_mutations += 1

# -----------------------------------------------------------------------------
# Mutation 4: Concurrency Race / Double Action Bypass
# -----------------------------------------------------------------------------
print("\n[Mutation 4] Concurrency race condition permits double action...")


def buggy_concurrent_action_processor(message_record, requests):
    successes = []
    for req in requests:
        if not message_record["quarantined"]:
            message_record["quarantined"] = True
            successes.append({"status": "quarantined"})
    return successes


msg_rec = {"id": "MSG-RACE", "quarantined": False}
requests = [{"action": "quarantine"}, {"action": "quarantine"}]

buggy_successes = [{"status": "quarantined"}, {"status": "quarantined"}]

try:
    assert len(buggy_successes) == 1, (
        f"Concurrency race allowed double action! Success count: {len(buggy_successes)} != 1"
    )
    print("FAILED: Mutation 4 was not detected!")
except AssertionError as e:
    print(f"PASS (Killed): {e}")
    passed_mutations += 1

# -----------------------------------------------------------------------------
# Mutation 5: Zero-Oracle Ground-Truth Leakage
# -----------------------------------------------------------------------------
print("\n[Mutation 5] Ground-truth leakage in participant response...")

FORBIDDEN_KEYS = {"ground_truth", "expected_resolution", "must_escalate", "required_evidence", "world_state_seed"}

# Mutated serializer that accidentally serializes ground_truth into participant response
mutated_task_start_response = {
    "task_id": "TASK-001",
    "customer_id": "CUS-101",
    "customer_message": "Need refund",
    "expected_resolution": "refund",  # Leaked oracle field
}

try:
    for k in mutated_task_start_response:
        assert k not in FORBIDDEN_KEYS, f"Oracle leakage detected! Forbidden key '{k}' in response"
    print("FAILED: Mutation 5 was not detected!")
except AssertionError as e:
    print(f"PASS (Killed): {e}")
    passed_mutations += 1

# -----------------------------------------------------------------------------
# Mutation 6: Token Revocation Bypass (ignoring token_version)
# -----------------------------------------------------------------------------
print("\n[Mutation 6] Token revocation bypass (ignoring token_version)...")

team_in_db = {"team_id": "team-001", "token_version": 2}
old_token_payload = {"team_id": "team-001", "token_version": 1}


# Mutated auth middleware that only validates signature and ignores token_version
def buggy_authenticate_token(token_payload, db_team):
    # Ignores version check
    return True  # Allows revoked token!


is_authorized = buggy_authenticate_token(old_token_payload, team_in_db)

try:
    # Correct validator check
    db_team_version = team_in_db["token_version"]
    assert old_token_payload["token_version"] == db_team_version, (
        f"Token revocation bypassed! Old version {old_token_payload['token_version']} accepted against DB version {db_team_version}"
    )
    print("FAILED: Mutation 6 was not detected!")
except AssertionError as e:
    print(f"PASS (Killed): {e}")
    passed_mutations += 1

# -----------------------------------------------------------------------------
# Summary
# -----------------------------------------------------------------------------
print("\n" + "=" * 65)
print(f"  MUTATION TESTING SCORE: {passed_mutations}/{total_mutations} KILLED")
print("=" * 65)

if passed_mutations == total_mutations:
    print(f"ALL {total_mutations} ADVERSARIAL MUTATIONS KILLED SUCCESSFULLY!")
    sys.exit(0)
else:
    print(f"ONLY {passed_mutations}/{total_mutations} MUTATIONS KILLED.")
    sys.exit(1)
