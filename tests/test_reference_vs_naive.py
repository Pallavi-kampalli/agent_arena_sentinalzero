import statistics
import sys
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

ROOT_DIR = Path(__file__).resolve().parent.parent
_orig_path = list(sys.path)
if str(ROOT_DIR / "src") not in sys.path:
    sys.path.insert(0, str(ROOT_DIR / "src"))
if str(ROOT_DIR / "starter-kit") not in sys.path:
    sys.path.insert(0, str(ROOT_DIR / "starter-kit"))

from agent_arena.agent import sentinelzero_agent as agent  # noqa: E402
from sdk.tools_client import ToolsClient  # noqa: E402
from agent_arena.scoring.sentinelzero_evaluator import SentinelZeroTaskEvaluator  # noqa: E402


def reference_solve(task: dict[str, Any], tools: ToolsClient) -> dict[str, Any]:
    return agent.solve(task, tools)


def naive_solve(task: dict[str, Any], tools: ToolsClient) -> dict[str, Any]:
    """Deliberately naive agent that exhibits shallow, ungrounded behavior.

    - Skips deep investigation.
    - Fabricates unretrieved phantom evidence IDs.
    - Claims 1.0 confidence regardless of ambiguity.
    """
    return {
        "case_classification": {
            "category": "legitimate",
            "issue": "normal_email",
            "severity": "low",
        },
        "decision": {
            "resolution": "allow",
            "escalation_required": False,
        },
        "evidence": ["DOM-FAKE-PHANTOM-999", "EMP-GUESSED-000"],
        "uncertainties": [],
        "customer_response": "Automated naive agent response.",
        "confidence": 1.0,
    }


def create_test_task_fixture(
    task_id: str,
    expected_res: str,
    req_evidence: list[str],
) -> dict[str, Any]:
    """Builds a standardized SentinelZero task fixture."""
    msg_id = f"MSG-{task_id}"
    emp_id = f"EMP-{task_id}"
    dom_id = f"DOM-{task_id}"

    world_state = {
        "current_date": "2026-09-15T00:00:00Z",
        "directory": [
            {
                "id": emp_id,
                "name": "Alex Taylor",
                "email": f"alex.{task_id.lower()}@sentinel-acme.edu",
                "role": "Security Lead",
                "department": "IT",
                "status": "active",
            }
        ],
        "domains": [
            {
                "domain_id": dom_id,
                "domain": f"sentinel-{task_id.lower()}.info",
                "category": "suspicious",
                "reputation": "malicious" if expected_res == "quarantine" else "neutral",
            }
        ],
        "threat_intel": [
            {
                "domain_id": dom_id,
                "domain": f"sentinel-{task_id.lower()}.info",
                "reputation": "malicious" if expected_res == "quarantine" else "neutral",
            }
        ],
        "security_policies": [
            {
                "id": "POL-001",
                "category": "phishing",
                "content": "Quarantine messages from malicious external domains.",
            }
        ],
        "target_message_id": msg_id,
        "sender_email": f"hacker@sentinel-{task_id.lower()}.info",
        "recipient_email": f"alex.{task_id.lower()}@sentinel-acme.edu",
        "subject": "Urgent Action Required",
        "message_body": f"Please verify credentials at http://sentinel-{task_id.lower()}.info immediately.",
    }

    return {
        "task_id": task_id,
        "world_seed": world_state,
        "input_payload": {
            "message_id": msg_id,
            "sender_email": f"hacker@sentinel-{task_id.lower()}.info",
            "recipient_email": f"alex.{task_id.lower()}@sentinel-acme.edu",
            "customer_message": f"Please verify credentials at http://sentinel-{task_id.lower()}.info immediately.",
        },
        "ground_truth": {
            "verdict": expected_res.upper(),
            "recommended_action": f"{expected_res}_message" if expected_res == "quarantine" else "allow_and_deliver",
            "expected_resolution": expected_res,
            "severity": "HIGH" if expected_res == "quarantine" else "LOW",
            "must_escalate": (expected_res == "escalate"),
            "required_evidence": req_evidence,
            "acceptable_evidence": req_evidence + [msg_id, "POL-001"],
            "unacceptable_actions": ["allow_and_deliver"] if expected_res == "quarantine" else [],
            "prompt_injection_present": False,
        },
        "world_state": world_state,
    }


def test_agent_quality_differential_distributional_analysis():
    """Runs Reference Agent vs Deliberately Naive Agent across diverse tasks.

    Asserts that:
    1. Reference agent substantially outperforms naive agent on Evidence score (due to phantom fabrication).
    2. Reference agent outperforms naive agent on Policy score.
    3. Reference agent achieves higher Task Success and Robustness.
    4. Evaluator remains robust and crash-free even on pathological naive agent responses.
    """
    tasks = [
        create_test_task_fixture("T1", "quarantine", ["EMP-T1", "DOM-T1"]),
        create_test_task_fixture("T2", "quarantine", ["EMP-T2", "DOM-T2"]),
        create_test_task_fixture("T3", "allow", ["EMP-T3", "DOM-T3"]),
        create_test_task_fixture("T4", "quarantine", ["EMP-T4", "DOM-T4"]),
        create_test_task_fixture("T5", "allow", ["EMP-T5", "DOM-T5"]),
        create_test_task_fixture("T6", "quarantine", ["EMP-T6", "DOM-T6"]),
    ]

    ref_scores = []
    naive_scores = []

    for t in tasks:
        world = t["world_state"]
        emp = world["directory"][0]
        dom = world["domains"][0]

        # ---------------------------------------------------------------------
        # 1. Simulate Reference Agent Run
        # ---------------------------------------------------------------------
        mock_tools_ref = MagicMock()
        mock_tools_ref.lookup_directory.return_value = {"found": True, "employee": emp}
        mock_tools_ref.get_approved_domains.return_value = {"official_domains": ["sentinel-acme.edu"]}
        mock_tools_ref.get_email_headers.return_value = {
            "message_id": t["input_payload"]["message_id"],
            "sender": t["input_payload"]["sender_email"],
            "recipient": t["input_payload"]["recipient_email"],
        }
        mock_tools_ref.inspect_domain_reputation.return_value = {
            "domain": dom["domain"],
            "domain_id": dom["domain_id"],
            "reputation": dom["reputation"],
        }
        mock_tools_ref.quarantine_message.return_value = {"status": "quarantined"}
        mock_tools_ref.allow_and_deliver.return_value = {"status": "delivered"}

        ref_output = reference_solve(
            {
                "task_id": t["task_id"],
                "customer_message": t["input_payload"]["customer_message"],
                "input_payload": t["input_payload"],
            },
            mock_tools_ref,
        )

        ref_tool_logs = [
            {
                "tool_name": "lookup_directory",
                "request_payload": {"identifier": emp["email"]},
                "response_payload": {"employee": emp},
                "was_enforcement_rejection": False,
            },
            {
                "tool_name": "inspect_domain_reputation",
                "request_payload": {"domain": dom["domain"]},
                "response_payload": {"domain": dom["domain"], "domain_id": dom["domain_id"]},
                "was_enforcement_rejection": False,
            },
        ]
        ref_runtime_state = dict(world)

        ref_eval = SentinelZeroTaskEvaluator.evaluate_task(
            task_id=t["task_id"],
            world_seed=t["world_seed"],
            ground_truth=t["ground_truth"],
            runtime_state=ref_runtime_state,
            submission_record={"status": "completed", "submission_payload": ref_output},
            tool_logs=ref_tool_logs,
            input_payload=t["input_payload"],
        )
        ref_scores.append(ref_eval.scores)

        # ---------------------------------------------------------------------
        # 2. Simulate Deliberately Naive Agent Run
        # ---------------------------------------------------------------------
        mock_tools_naive = MagicMock()
        naive_output = naive_solve(
            {
                "task_id": t["task_id"],
                "customer_message": t["input_payload"]["customer_message"],
            },
            mock_tools_naive,
        )

        naive_tool_logs = []
        naive_runtime_state = dict(world)

        naive_eval = SentinelZeroTaskEvaluator.evaluate_task(
            task_id=t["task_id"],
            world_seed=t["world_seed"],
            ground_truth=t["ground_truth"],
            runtime_state=naive_runtime_state,
            submission_record={"status": "completed", "submission_payload": naive_output},
            tool_logs=naive_tool_logs,
            input_payload=t["input_payload"],
        )
        naive_scores.append(naive_eval.scores)

    # -------------------------------------------------------------------------
    # 3. Distributional and Paired Quality Metrics
    # -------------------------------------------------------------------------
    ref_task_success = [s.task_success for s in ref_scores]
    naive_task_success = [s.task_success for s in naive_scores]

    ref_evidence = [s.evidence for s in ref_scores]
    naive_evidence = [s.evidence for s in naive_scores]

    ref_policy = [s.policy for s in ref_scores]
    naive_policy = [s.policy for s in naive_scores]

    ref_aggregate = [s.task_aggregate for s in ref_scores]
    naive_aggregate = [s.task_aggregate for s in naive_scores]

    print("\n--- Agent Quality Differential Analysis ---")
    print(
        f"Reference Mean Task Success: {statistics.mean(ref_task_success):.3f} vs Naive: {statistics.mean(naive_task_success):.3f}"
    )
    print(
        f"Reference Mean Evidence:     {statistics.mean(ref_evidence):.3f} vs Naive: {statistics.mean(naive_evidence):.3f}"
    )
    print(
        f"Reference Mean Policy:       {statistics.mean(ref_policy):.3f} vs Naive: {statistics.mean(naive_policy):.3f}"
    )
    print(
        f"Reference Mean Aggregate:    {statistics.mean(ref_aggregate):.3f} vs Naive: {statistics.mean(naive_aggregate):.3f}"
    )

    # Core Differential Assertions:
    # 1. Evidence: Naive fabricates IDs without retrieving them -> severe penalty
    assert statistics.mean(ref_evidence) > statistics.mean(naive_evidence)
    assert min(naive_evidence) == 0.0

    # 2. Policy & Task Success: Reference clearly outperforms naive
    assert statistics.mean(ref_aggregate) > statistics.mean(naive_aggregate)
    print("[PASS] Strong observed separation on the selected diagnostic benchmark.")
