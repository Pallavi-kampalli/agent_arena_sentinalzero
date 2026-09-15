import statistics
import sys
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

ROOT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT_DIR / "src"))
sys.path.insert(0, str(ROOT_DIR / "starter-kit"))

from agent import solve as reference_solve  # noqa: E402
from sdk.tools_client import ToolsClient  # noqa: E402

from agent_arena.scoring.evaluator import TaskEvaluator  # noqa: E402


def naive_solve(task: dict[str, Any], tools: ToolsClient) -> dict[str, Any]:
    """Deliberately naive agent that exhibits shallow, ungrounded behavior.

    - Skips deep investigation.
    - Fabricates unretrieved phantom evidence IDs.
    - Attempts premature actions without policy verification.
    - Claims 1.0 confidence regardless of ambiguity.
    """
    # Naive agent attempts an action blindly
    try:
        tools.issue_refund(transaction_id="TXN-BLIND-001", amount=1000.0, reason="I think you want money")
    except Exception:
        pass

    # Fabricates phantom IDs that were never retrieved
    return {
        "case_classification": {
            "category": "other",
            "issue": "random_issue",
            "severity": "low",
        },
        "decision": {
            "resolution": "refund",
            "escalation_required": False,
        },
        "evidence": ["DOC-FAKE-PHANTOM-999", "TXN-GUESSED-000"],
        "uncertainties": [],
        "customer_response": "Here is your refund.",
        "confidence": 1.0,
    }


def create_test_task_fixture(
    task_id: str,
    family: str,
    variant: str,
    expected_res: str,
    req_evidence: list[str],
    has_chargeback_hold: bool = False,
) -> dict[str, Any]:
    """Builds a standardized task fixture covering diverse families and variants."""
    cust_id = f"CUS-{task_id}"
    txn_id = f"TXN-{task_id}"
    doc_id = f"DOC-{task_id}"

    world_state = {
        "current_date": "2026-09-15T00:00:00Z",
        "customers": [
            {
                "id": cust_id,
                "name": "Test Customer",
                "tier": "pro",
                "region": "NA",
                "verification_status": "verified",
                "account_status": "active",
                "created_at": "2026-01-01T00:00:00Z",
            }
        ],
        "transactions": [
            {
                "id": txn_id,
                "customer_id": cust_id,
                "amount": 99.0,
                "currency": "USD",
                "status": "completed",
                "created_at": "2026-09-10T00:00:00Z",
                "refunded_amount": 0.0,
                "chargeback_status": "inquiry" if has_chargeback_hold else None,
            }
        ],
        "subscriptions": [
            {
                "id": f"SUB-{task_id}",
                "customer_id": cust_id,
                "plan": "monthly",
                "status": "active",
                "lock_in_until": None,
                "created_at": "2026-01-01T00:00:00Z",
            }
        ],
        "knowledge_documents": [
            {
                "id": doc_id,
                "title": "Authoritative Policy",
                "content": "Refunds permitted within 30 days unless chargeback inquiry is active.",
                "category": "refund",
                "updated_at": "2026-09-01T00:00:00Z",
            }
        ],
        "previous_cases": [],
        "escalations": [],
    }

    msg = f"Please refund my purchase {txn_id} for 99.0 dollars."
    if expected_res == "refund":
        exp_action = {"tool": "issue_refund", "params": {"transaction_id": txn_id, "amount": 99.0}}
    else:
        exp_action = {"tool": "escalate_case", "params": {"case_id": task_id}}

    return {
        "task_id": task_id,
        "family": family,
        "variant": variant,
        "world_seed": world_state,
        "input_payload": {
            "customer_id": cust_id,
            "customer_message": msg,
        },
        "ground_truth": {
            "expected_resolution": expected_res,
            "must_escalate": (expected_res == "escalate"),
            "required_evidence": req_evidence,
            "expected_action": exp_action,
        },
        "world_state": world_state,
    }


def test_agent_quality_differential_distributional_analysis():
    """Runs Reference Agent vs Deliberately Naive Agent across diverse tasks.

    Asserts that:
    1. Reference agent substantially outperforms naive agent on Evidence score (due to phantom fabrication).
    2. Reference agent outperforms naive agent on Policy score (due to ungrounded actions).
    3. Reference agent achieves higher Task Success and Robustness.
    4. Evaluator remains robust and crash-free even on pathological naive agent responses.
    """
    tasks = [
        create_test_task_fixture("T1", "refund_request", "normal", "refund", ["TXN-T1", "DOC-T1"]),
        create_test_task_fixture("T2", "refund_request", "distractor", "refund", ["TXN-T2", "DOC-T2"]),
        create_test_task_fixture(
            "T3", "refund_request", "adversarial", "escalate", ["TXN-T3", "DOC-T3"], has_chargeback_hold=True
        ),
        create_test_task_fixture(
            "T4", "refund_request", "contradiction", "escalate", ["TXN-T4", "DOC-T4"], has_chargeback_hold=True
        ),
        create_test_task_fixture("T5", "duplicate_payment", "missing_info", "refund", ["TXN-T5", "DOC-T5"]),
        create_test_task_fixture("T6", "previous_agent_was_wrong", "stale", "refund", ["TXN-T6", "DOC-T6"]),
    ]

    ref_scores = []
    naive_scores = []

    for t in tasks:
        world = t["world_state"]
        cust_id = t["input_payload"]["customer_id"]
        txn = world["transactions"][0]
        doc = world["knowledge_documents"][0]

        # ---------------------------------------------------------------------
        # 1. Simulate Reference Agent Run
        # ---------------------------------------------------------------------
        mock_tools_ref = MagicMock()
        mock_tools_ref.get_customer.return_value = {"customer": world["customers"][0]}
        mock_tools_ref.get_transactions.return_value = {"transactions": world["transactions"]}
        mock_tools_ref.search_knowledge.return_value = {"results": [{"id": doc["id"]}]}
        mock_tools_ref.get_document.return_value = {"document": doc}

        if txn["chargeback_status"] == "inquiry":
            mock_tools_ref.issue_refund.return_value = {
                "error": "INELIGIBLE",
                "reason": "chargeback_investigation_active",
                "policy_ref": doc["id"],
            }
            mock_tools_ref.escalate_case.return_value = {"status": "escalated"}
        else:
            mock_tools_ref.issue_refund.return_value = {"status": "success", "transaction": txn}

        ref_output = reference_solve(
            {
                "task_id": t["task_id"],
                "customer_id": cust_id,
                "customer_message": t["input_payload"]["customer_message"],
            },
            mock_tools_ref,
        )

        ref_tool_logs = [
            {
                "tool_name": "get_customer",
                "request_payload": {},
                "response_payload": {"customer": world["customers"][0]},
                "was_enforcement_rejection": False,
            },
            {
                "tool_name": "get_transactions",
                "request_payload": {},
                "response_payload": {"transactions": world["transactions"]},
                "was_enforcement_rejection": False,
            },
            {
                "tool_name": "get_document",
                "request_payload": {},
                "response_payload": {"document": doc},
                "was_enforcement_rejection": False,
            },
        ]
        if txn["chargeback_status"] == "inquiry":
            ref_tool_logs.append(
                {
                    "tool_name": "issue_refund",
                    "request_payload": {},
                    "response_payload": {"error": "INELIGIBLE"},
                    "was_enforcement_rejection": True,
                }
            )
            ref_tool_logs.append(
                {
                    "tool_name": "escalate_case",
                    "request_payload": {},
                    "response_payload": {"status": "escalated"},
                    "was_enforcement_rejection": False,
                }
            )
            ref_runtime_state = dict(world)
            ref_runtime_state["escalations"] = [{"case_id": t["task_id"], "team": "billing_specialists"}]
        else:
            ref_tool_logs.append(
                {
                    "tool_name": "issue_refund",
                    "request_payload": {},
                    "response_payload": {"status": "success"},
                    "was_enforcement_rejection": False,
                }
            )
            ref_runtime_state = dict(world)
            ref_runtime_state["transactions"] = [dict(txn, refunded_amount=txn["amount"], refund_status="refunded")]

        ref_eval = TaskEvaluator.evaluate_task(
            task_id=t["task_id"],
            family=t["family"],
            variant=t["variant"],
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
        mock_tools_naive.issue_refund.return_value = {"error": "INELIGIBLE", "reason": "transaction_not_found"}
        naive_output = naive_solve(
            {
                "task_id": t["task_id"],
                "customer_id": cust_id,
                "customer_message": t["input_payload"]["customer_message"],
            },
            mock_tools_naive,
        )

        naive_tool_logs = [
            {
                "tool_name": "issue_refund",
                "request_payload": {},
                "response_payload": {"error": "INELIGIBLE"},
                "was_enforcement_rejection": True,
            }
        ]
        naive_runtime_state = dict(world)

        naive_eval = TaskEvaluator.evaluate_task(
            task_id=t["task_id"],
            family=t["family"],
            variant=t["variant"],
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

    # 2. Policy: Naive acts blindly without verifying policy -> lower policy
    assert statistics.mean(ref_policy) > statistics.mean(naive_policy)

    # 3. Task Success & Aggregate: Reference clearly outperforms naive
    assert statistics.mean(ref_task_success) > statistics.mean(naive_task_success)
    assert statistics.mean(ref_aggregate) > statistics.mean(naive_aggregate)

    # 4. Robustness: Strong observed separation on non-normal variants (distractor, adversarial, contradiction, missing_info, stale)
    non_normal_indices = [i for i, t in enumerate(tasks) if t["variant"] != "normal"]
    ref_robustness = [ref_scores[i].robustness for i in non_normal_indices]
    naive_robustness = [naive_scores[i].robustness for i in non_normal_indices]

    print("\n--- Non-Normal Variant Robustness Separation ---")
    print(f"Non-normal variants evaluated: {[tasks[i]['variant'] for i in non_normal_indices]}")
    print(f"Reference Mean Robustness (Non-Normal): {statistics.mean(ref_robustness):.3f}")
    print(f"Naive Mean Robustness (Non-Normal):     {statistics.mean(naive_robustness):.3f}")

    assert statistics.mean(ref_robustness) > statistics.mean(naive_robustness)
    assert statistics.mean(ref_robustness) == 1.000
    assert statistics.mean(naive_robustness) == 0.000
    print("[PASS] Strong observed separation on the selected diagnostic benchmark.")

    # 5. Paired task win rate: Reference wins on every task
    wins = sum(1 for r, n in zip(ref_aggregate, naive_aggregate, strict=False) if r > n)
    assert wins >= len(tasks) * 0.8  # At least 80% clear wins
