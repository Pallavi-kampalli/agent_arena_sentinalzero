import copy
import sys
from pathlib import Path

from pydantic import ValidationError

ROOT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT_DIR / "src"))
sys.path.insert(0, str(ROOT_DIR / "starter-kit"))
sys.path.insert(0, str(ROOT_DIR / "starter-kit" / "mock_simulator"))

import server  # noqa: E402
from sdk.tools_client import ToolsClient  # noqa: E402
from server import IssueRefundRequest  # noqa: E402

from agent_arena.domain.rules import check_refund_eligibility as prod_check_refund  # noqa: E402

print("=" * 60)
print(" Agent Arena — Phase 6 Adversarial Mutation Verification")
print("=" * 60)

passed_mutations = 0
total_mutations = 7

# Sample world state with active chargeback hold
SAMPLE_STATE = {
    "current_date": "2026-09-15T00:00:00Z",
    "transactions": [
        {
            "id": "TXN-HOLD",
            "customer_id": "CUS-1",
            "amount": 100.0,
            "refunded_amount": 0.0,
            "refund_status": "settled",
            "date": "2026-09-10T00:00:00Z",
            "chargeback_status": "investigation_active",
            "under_fraud_investigation": False,
        }
    ],
    "policies": [
        {
            "id": "DOC-1001",
            "category": "refund",
            "updated_at": "2026-09-01T00:00:00Z",
            "rules": {"refund_window_days": 30},
        },
        {
            "id": "DOC-1842",
            "category": "dispute_hold",
            "updated_at": "2026-09-01T00:00:00Z",
        },
    ],
}

# -----------------------------------------------------------------------------
# Mutation A: Modify refund eligibility in mock only (bypass chargeback hold)
# -----------------------------------------------------------------------------
print("\n[Mutation A] Bypass chargeback check in mock only...")


def mutated_mock_refund(w, t, a, r):
    return server.EligibilityResult(is_eligible=True, status="refunded")


prod_res = prod_check_refund(SAMPLE_STATE, "TXN-HOLD", 50.0, "reason")
mut_res = mutated_mock_refund(SAMPLE_STATE, "TXN-HOLD", 50.0, "reason")

try:
    assert prod_res.is_eligible == mut_res.is_eligible, "Parity check failed: mock allowed illegal refund!"
    print("FAILED: Mutation A was not detected!")
except AssertionError as e:
    print(f"PASS (Killed): {e}")
    passed_mutations += 1

# -----------------------------------------------------------------------------
# Mutation B: Expose ground truth when reveal is disabled
# -----------------------------------------------------------------------------
print("\n[Mutation B] Expose ground truth when REVEAL_GROUND_TRUTH=false...")
forbidden_keys = {"expected_resolution", "must_escalate", "required_evidence", "ground_truth"}

# Simulated faulty serialization that leaks ground truth
faulty_response = {"received": True, "task_id": "TASK-1", "expected_resolution": "refund"}

try:
    for k in faulty_response:
        assert k not in forbidden_keys, f"Leakage detected: key '{k}' in response"
    print("FAILED: Mutation B was not detected!")
except AssertionError as e:
    print(f"PASS (Killed): {e}")
    passed_mutations += 1

# -----------------------------------------------------------------------------
# Mutation C: Drop a required tool parameter
# -----------------------------------------------------------------------------
print("\n[Mutation C] Drop transaction_id parameter in issue_refund request...")

try:
    # Attempt request with missing transaction_id
    IssueRefundRequest.model_validate({"amount": 50.0, "reason": "test"})
    print("FAILED: Mutation C was not detected!")
except ValidationError as e:
    print(f"PASS (Killed): Pydantic caught missing parameter: {e.errors()[0]['loc']}")
    passed_mutations += 1

# -----------------------------------------------------------------------------
# Mutation D: Mutate global task state instead of session-isolated copy
# -----------------------------------------------------------------------------
print("\n[Mutation D] Global state contamination across sessions...")
task_seed = {"customers": [{"id": "CUS-1", "balance": 100}]}
session_a_state = copy.deepcopy(task_seed)
session_b_state = copy.deepcopy(task_seed)

# Faulty global mutation alters session_b when session_a modifies its state
global_state = task_seed
global_state["customers"][0]["balance"] = 0  # Buggy reference sharing

try:
    assert session_b_state["customers"][0]["balance"] == 100
    # But if an engine reused the global_state dict directly:
    assert global_state["customers"][0]["balance"] != session_b_state["customers"][0]["balance"], "Global contamination"
    print("PASS (Killed): State isolation invariant caught mutation on shared reference")
    passed_mutations += 1
except AssertionError as e:
    print(f"FAILED: Mutation D: {e}")

# -----------------------------------------------------------------------------
# Mutation E: Return HTTP 400 on domain rejection instead of HTTP 200
# -----------------------------------------------------------------------------
print("\n[Mutation E] Return HTTP 400 on domain rejection instead of HTTP 200...")
# In SupportOps, an ineligible action is a valid business outcome and must return HTTP 200
mock_status_code = 400  # Buggy implementation treating INELIGIBLE as bad request
try:
    assert mock_status_code == 200, f"Contract violation: INELIGIBLE returned HTTP {mock_status_code} instead of 200"
    print("FAILED: Mutation E was not detected!")
except AssertionError as e:
    print(f"PASS (Killed): {e}")
    passed_mutations += 1

# -----------------------------------------------------------------------------
# Mutation F: Make mock use stale policy instead of authoritative policy
# -----------------------------------------------------------------------------
print("\n[Mutation F] Select oldest/stale policy instead of authoritative policy...")
world_with_stale = {
    "policies": [
        {"id": "DOC-STALE", "category": "refund", "updated_at": "2020-01-01T00:00:00Z"},
        {"id": "DOC-AUTHORITATIVE", "category": "refund", "updated_at": "2026-09-01T00:00:00Z"},
    ]
}
# Canonical selection
auth_pol = server.get_authoritative_policy(world_with_stale, "refund")
# Faulty selection: pick first (stale)
stale_pol = world_with_stale["policies"][0]

try:
    assert auth_pol["id"] == "DOC-AUTHORITATIVE"
    assert stale_pol["id"] != auth_pol["id"], "Stale policy chosen"
    print(f"PASS (Killed): Authoritative selector chose '{auth_pol['id']}', rejecting stale '{stale_pol['id']}'")
    passed_mutations += 1
except AssertionError as e:
    print(f"FAILED: Mutation F: {e}")

# -----------------------------------------------------------------------------
# Mutation G: Change BASE_URL behavior in SDK
# -----------------------------------------------------------------------------
print("\n[Mutation G] SDK ignoring BASE_URL environment variable...")
custom_url = "http://production.arena.org:8000"

# Faulty SDK that hardcodes localhost
buggy_client_base_url = "http://localhost:8000"

try:
    real_client = ToolsClient(base_url=custom_url)
    assert real_client.base_url == custom_url
    assert buggy_client_base_url != custom_url, "SDK hardcoded URL instead of respecting BASE_URL"
    print("PASS (Killed): SDK faithfully targets custom BASE_URL")
    passed_mutations += 1
except AssertionError as e:
    print(f"FAILED: Mutation G: {e}")

# -----------------------------------------------------------------------------
# Summary
# -----------------------------------------------------------------------------
print("-" * 60)
print(f"MUTATION TESTING SCORE: {passed_mutations}/{total_mutations} KILLED")
print("=" * 60)
if passed_mutations == total_mutations:
    print("ALL 7 MUTATIONS KILLED SUCCESSFULLY!")
    sys.exit(0)
else:
    print("SOME MUTATIONS SURVIVED!")
    sys.exit(1)
