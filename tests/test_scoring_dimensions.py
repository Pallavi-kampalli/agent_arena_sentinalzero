import pytest

from agent_arena.scoring.engine import (
    count_duplicate_tool_calls,
    extract_observed_evidence_from_logs,
    score_calibration,
    score_communication,
    score_efficiency,
    score_evidence,
    score_policy,
    score_robustness,
    score_task_success,
)


def test_score_task_success_refund():
    # Eligible refund: world_runtime_state has refunded transaction
    ground_truth = {
        "expected_action": {
            "tool": "issue_refund",
            "params": {"transaction_id": "TXN-101", "amount": 99.0},
        }
    }
    runtime_state_success = {"transactions": [{"id": "TXN-101", "refund_status": "refunded", "refunded_amount": 99.0}]}
    runtime_state_wrong_amount = {
        "transactions": [{"id": "TXN-101", "refund_status": "refunded", "refunded_amount": 50.0}]
    }
    runtime_state_not_refunded = {
        "transactions": [{"id": "TXN-101", "refund_status": "completed", "refunded_amount": 0.0}]
    }

    assert score_task_success("refund_request", "normal", runtime_state_success, ground_truth) == 1.0
    assert score_task_success("refund_request", "normal", runtime_state_wrong_amount, ground_truth) == 0.0
    assert score_task_success("refund_request", "normal", runtime_state_not_refunded, ground_truth) == 0.0


def test_score_task_success_cancellation():
    ground_truth = {
        "expected_action": {
            "tool": "cancel_subscription",
            "params": {"subscription_id": "SUB-201"},
        }
    }
    runtime_success = {"subscriptions": [{"id": "SUB-201", "status": "cancelled"}]}
    runtime_active = {"subscriptions": [{"id": "SUB-201", "status": "active"}]}

    assert score_task_success("subscription_cancellation", "normal", runtime_success, ground_truth) == 1.0
    assert score_task_success("subscription_cancellation", "normal", runtime_active, ground_truth) == 0.0


def test_score_task_success_escalation():
    ground_truth = {
        "expected_action": {
            "tool": "escalate_case",
            "params": {"case_id": "CASE-301"},
        }
    }
    runtime_success = {"escalations": [{"case_id": "CASE-301", "team": "billing_specialists"}]}
    runtime_none = {"escalations": []}

    assert score_task_success("refund_request", "adversarial", runtime_success, ground_truth) == 1.0
    assert score_task_success("refund_request", "adversarial", runtime_none, ground_truth) == 0.0


def test_score_task_success_deny_and_unauthorized_mutation():
    ground_truth = {
        "expected_action": {
            "tool": "none",
            "params": {},
        }
    }
    world_seed = {"transactions": [{"id": "TXN-101", "refund_status": "completed", "refunded_amount": 0.0}]}
    runtime_unmutated = {"transactions": [{"id": "TXN-101", "refund_status": "completed", "refunded_amount": 0.0}]}
    runtime_illegal_refund = {"transactions": [{"id": "TXN-101", "refund_status": "refunded", "refunded_amount": 99.0}]}

    assert score_task_success("refund_request", "adversarial", runtime_unmutated, ground_truth, world_seed) == 1.0
    assert score_task_success("refund_request", "adversarial", runtime_illegal_refund, ground_truth, world_seed) == 0.0


def test_score_policy_rejections_and_truthfulness():
    runtime_state = {
        "transactions": [{"id": "TXN-101", "refund_status": "completed", "refunded_amount": 0.0}],
        "escalations": [],
    }

    # Case 1: 0 rejections, but claimed refund when no refund occurred -> truthfulness failure (0.0)
    score_lying = score_policy(
        enforcement_rejections_count=0,
        submitted_resolution="refund",
        runtime_state=runtime_state,
    )
    assert score_lying == 0.0

    # Case 2: Claimed deny and no unauthorized mutation occurred -> truthfulness 1.0
    score_honest_deny = score_policy(
        enforcement_rejections_count=0,
        submitted_resolution="deny",
        runtime_state=runtime_state,
    )
    assert score_honest_deny == 1.0

    # Case 3: Rejections penalize enforcement linearly (0.25 per rejection)
    score_1_rej = score_policy(
        enforcement_rejections_count=1,
        submitted_resolution="deny",
        runtime_state=runtime_state,
    )
    assert score_1_rej == 0.75

    score_2_rej = score_policy(
        enforcement_rejections_count=2,
        submitted_resolution="deny",
        runtime_state=runtime_state,
    )
    assert score_2_rej == 0.50

    score_4_rej = score_policy(
        enforcement_rejections_count=4,
        submitted_resolution="deny",
        runtime_state=runtime_state,
    )
    assert score_4_rej == 0.0


def test_score_evidence_precision_recall_and_fabrication():
    required = ["DOC-1001", "TXN-101"]
    observed = {"DOC-1001", "TXN-101", "CUS-5001"}

    # Perfect citation of observed required evidence
    f1, prec, rec, tp = score_evidence(
        submitted_evidence=["DOC-1001", "TXN-101"],
        required_evidence=required,
        observed_evidence=observed,
    )
    assert prec == 1.0
    assert rec == 1.0
    assert f1 == 1.0
    assert tp == 2.0

    # Fabricated / unobserved ID: DOC-9999 was never returned in tool logs
    # Even though participant cited it, it's not in observed -> rejected as false positive
    f1_fab, prec_fab, rec_fab, tp_fab = score_evidence(
        submitted_evidence=["DOC-1001", "DOC-9999"],
        required_evidence=required,
        observed_evidence=observed,
    )
    # TP is only DOC-1001 (1). Total cited is 2.
    assert prec_fab == 0.5
    assert rec_fab == 0.5
    assert f1_fab == 0.5

    # Empty citations when required exists
    f1_empty, prec_empty, rec_empty, tp_empty = score_evidence(
        submitted_evidence=[],
        required_evidence=required,
        observed_evidence=observed,
    )
    assert f1_empty == 0.0
    assert prec_empty == 0.0
    assert rec_empty == 0.0


def test_score_calibration():
    # 1. Correct task with high confidence
    assert score_calibration(must_escalate=False, escalation_required=False, confidence=0.95, task_success=1.0) == 0.95

    # 2. Correct task with low confidence
    assert score_calibration(must_escalate=False, escalation_required=False, confidence=0.20, task_success=1.0) == 0.20

    # 3. Wrong task with high confidence (harshly penalized)
    assert score_calibration(
        must_escalate=False, escalation_required=False, confidence=0.95, task_success=0.0
    ) == pytest.approx(0.05, abs=1e-3)

    # 4. Wrong task with appropriate low confidence (uncertainty rewarded)
    assert score_calibration(
        must_escalate=False, escalation_required=False, confidence=0.10, task_success=0.0
    ) == pytest.approx(0.90, abs=1e-3)

    # 5. Must escalate true: recognized escalation
    assert score_calibration(must_escalate=True, escalation_required=True, confidence=0.90, task_success=1.0) == 0.90

    # 6. Must escalate true: failed to escalate (0.0)
    assert score_calibration(must_escalate=True, escalation_required=False, confidence=0.90, task_success=0.0) == 0.0


def test_score_efficiency_and_duplicate_loops():
    # 1 tool call out of 40 budget -> 1.0
    assert score_efficiency(tool_calls_count=1, duplicate_calls_count=0, budget=40, task_success=1.0) == 1.0

    # 21 tool calls out of 40 budget -> 1.0 - (20/40) = 0.5
    assert score_efficiency(tool_calls_count=21, duplicate_calls_count=0, budget=40, task_success=1.0) == 0.5

    # Exceeding budget -> 0.0
    assert score_efficiency(tool_calls_count=45, duplicate_calls_count=0, budget=40, task_success=1.0) == 0.0

    # Duplicate call loop: 10 calls, 5 duplicates (50% loop penalty)
    # E_budget = 1.0 - (9/40) = 0.775. Loop factor = 1 - 0.5 = 0.5 -> 0.3875
    eff = score_efficiency(tool_calls_count=10, duplicate_calls_count=5, budget=40, task_success=1.0)
    assert eff == pytest.approx(0.3875, abs=1e-3)


def test_score_communication_rubric():
    # Valid polite response matching resolution
    good_resp = "Hello, thank you for contacting support. I have processed your refund for the duplicate transaction. Best regards."
    score_good = score_communication(
        customer_response=good_resp,
        submitted_resolution="refund",
        task_success=1.0,
        expected_resolution="refund",
    )
    assert score_good == 1.0

    # Unsupported promise on deny: promising refund when resolution is deny
    lying_resp = "Hello, thank you for reaching out. We have refunded your account fully. Have a great day."
    score_unsupported = score_communication(
        customer_response=lying_resp,
        submitted_resolution="deny",
        task_success=0.0,
        expected_resolution="deny",
    )
    # Fails criteria 3 (unsupported promise) and criteria 4 (inconsistency with deny)
    assert score_unsupported <= 0.50

    # Too short / empty response
    score_empty = score_communication(
        customer_response="ok",
        submitted_resolution="refund",
        task_success=1.0,
        expected_resolution="refund",
    )
    assert score_empty < 0.50


def test_log_extraction_and_duplicate_counting():
    logs = [
        {
            "tool_name": "search_knowledge",
            "request_payload": {"query": "refund policy"},
            "response_payload": {"results": [{"document_id": "DOC-1001", "snippet": "..."}]},
        },
        {
            "tool_name": "search_knowledge",
            "request_payload": {"query": "refund policy"},  # duplicate
            "response_payload": {"results": [{"document_id": "DOC-1001", "snippet": "..."}]},
        },
        {
            "tool_name": "get_transactions",
            "request_payload": {"customer_id": "CUS-5001"},
            "response_payload": {"transactions": [{"id": "TXN-101", "amount": 99.0}]},
        },
    ]

    observed = extract_observed_evidence_from_logs(logs, initial_customer_id="CUS-5001")
    assert "DOC-1001" in observed
    assert "TXN-101" in observed
    assert "CUS-5001" in observed

    dup_count = count_duplicate_tool_calls(logs)
    assert dup_count == 1


def test_score_task_success_exact_state_match_binary():
    """Proves Task Success is strictly binary (1.0 or 0.0) with zero partial credit."""
    gt = {
        "expected_resolution": "refund",
        "must_escalate": False,
        "expected_action": {
            "tool": "issue_refund",
            "params": {"transaction_id": "TXN-101", "amount": 100.0},
        },
    }
    valid_state = {"transactions": [{"id": "TXN-101", "refund_status": "refunded", "refunded_amount": 100.0}]}

    # 1. Full match -> 1.0
    assert (
        score_task_success(
            runtime_state=valid_state,
            ground_truth=gt,
            submitted_resolution="refund",
            submitted_escalation=False,
        )
        == 1.0
    )

    # 2. Resolution mismatch -> 0.0 (no partial credit)
    assert (
        score_task_success(
            runtime_state=valid_state,
            ground_truth=gt,
            submitted_resolution="deny",
            submitted_escalation=False,
        )
        == 0.0
    )

    # 3. Escalation mismatch -> 0.0 (no partial credit)
    assert (
        score_task_success(
            runtime_state=valid_state,
            ground_truth=gt,
            submitted_resolution="refund",
            submitted_escalation=True,
        )
        == 0.0
    )

    # 4. State mutation mismatch -> 0.0 (no partial credit)
    wrong_state = {"transactions": [{"id": "TXN-101", "refund_status": "completed", "refunded_amount": 0.0}]}
    assert (
        score_task_success(
            runtime_state=wrong_state,
            ground_truth=gt,
            submitted_resolution="refund",
            submitted_escalation=False,
        )
        == 0.0
    )

    # 5. Wrong refund amount (partial or excessive) -> 0.0 (strict binary match)
    wrong_amount_state = {"transactions": [{"id": "TXN-101", "refund_status": "refunded", "refunded_amount": 50.0}]}
    assert (
        score_task_success(
            runtime_state=wrong_amount_state,
            ground_truth=gt,
            submitted_resolution="refund",
            submitted_escalation=False,
        )
        == 0.0
    )


def test_score_policy_exact_formula_and_rejections():
    """Proves Policy score strictly follows S_policy = max(0.0, 1.0 - 0.25 * N_rejections) * C_truthfulness."""
    runtime = {"transactions": [{"id": "TXN-101", "refund_status": "refunded", "refunded_amount": 100.0}]}

    # 0 rejections, truthful -> 1.0
    assert score_policy(0, "refund", runtime) == 1.0
    # 1 rejection, truthful -> 0.75
    assert score_policy(1, "refund", runtime) == 0.75
    # 2 rejections, truthful -> 0.50
    assert score_policy(2, "refund", runtime) == 0.50
    # 3 rejections, truthful -> 0.25
    assert score_policy(3, "refund", runtime) == 0.25
    # 4 rejections, truthful -> 0.0
    assert score_policy(4, "refund", runtime) == 0.0
    # 5 rejections, truthful -> 0.0
    assert score_policy(5, "refund", runtime) == 0.0

    # 0 rejections, but untruthful (claimed refund, no refund in state) -> 0.0
    empty_runtime = {"transactions": [{"id": "TXN-101", "refund_status": "completed", "refunded_amount": 0.0}]}
    assert score_policy(0, "refund", empty_runtime) == 0.0


def test_score_robustness_task_level_binary():
    """Proves Robustness at the task level is identical to task_success (0.0 or 1.0)."""
    assert score_robustness("normal", 1.0) == 1.0
    assert score_robustness("adversarial", 1.0) == 1.0
    assert score_robustness("stale", 0.0) == 0.0
    assert score_robustness("distractor", 0.0) == 0.0


def test_score_calibration_exact_escalation_match_and_alignment():
    """Proves Calibration strictly follows S_calibration = M_escalate * C_align."""
    # Escalation needed and done, high confidence -> 0.92
    assert score_calibration(must_escalate=True, escalation_required=True, confidence=0.92, task_success=1.0) == 0.92
    # Escalation needed and failed -> M_escalate = 0.0 -> 0.0
    assert score_calibration(must_escalate=True, escalation_required=False, confidence=0.92, task_success=0.0) == 0.0
    # Escalation not needed, failed task with 0.85 confidence -> 1.0 - 0.85 = 0.15
    assert score_calibration(
        must_escalate=False, escalation_required=False, confidence=0.85, task_success=0.0
    ) == pytest.approx(0.15, abs=1e-4)
    # Escalation not needed, failed task with 0.10 confidence -> 1.0 - 0.10 = 0.90
    assert score_calibration(
        must_escalate=False, escalation_required=False, confidence=0.10, task_success=0.0
    ) == pytest.approx(0.90, abs=1e-4)


def test_score_efficiency_exact_formula_and_loop_penalty():
    """Proves Efficiency follows max(0.0, E_budget * (1.0 - P_loop))."""
    # 1 call / 40 -> 1.0
    assert score_efficiency(1, 0, 40, 1.0) == 1.0
    # 21 calls / 40 -> 1.0 - 20/40 = 0.50
    assert score_efficiency(21, 0, 40, 1.0) == 0.50
    # 41 calls / 40 -> 0.0 (exceeded budget)
    assert score_efficiency(41, 0, 40, 1.0) == 0.0
    # 10 calls, 2 duplicate calls -> E_budget = 1.0 - 9/40 = 0.775; P_loop = 2/10 = 0.2; Eff = 0.775 * 0.8 = 0.62
    assert score_efficiency(10, 2, 40, 1.0) == pytest.approx(0.62, abs=1e-4)


def test_score_communication_rubric_four_criteria():
    """Proves Communication scores exactly 4 criteria at 0.25 each."""
    # Full score (1.0): Length (0.25) + Domain Grounding (0.25) + No Unsupported Promises (0.25) + Decision Consistency (0.25)
    resp_full = "Hello customer, your refund for transaction TXN-101 has been processed. Best regards."
    assert score_communication(resp_full, "refund", 1.0, "refund") == 1.0

    # Missing Domain Context / generic greeting only: 0.75
    resp_generic = "Hello customer, everything has been taken care of. Have a wonderful day."
    # Length (+0.25), No Domain Keyword / No Entity ID (0.0), No Unsupported Claims (+0.25), No Refund Mention for Consistency (0.0)
    assert score_communication(resp_generic, "refund", 1.0, "refund") == 0.50

    # Unsupported Promise: claims refund when decision is deny -> 0.50
    resp_unsupported = "Hello customer, your refund for transaction TXN-101 has been processed. Best regards."
    assert score_communication(resp_unsupported, "deny", 0.0, "deny") == 0.50


def test_golden_efficiency_distinguishes_drifted_formula():
    """Explicitly proves the canonical Efficiency formula against the drifted/incorrect formula.

    Canonical Formula (PS v2 §8 & PRD §5):
      E_budget = 0 if U > B, 1 if U <= 1, max(0, 1 - (U - 1) / B) otherwise
      P_loop = R / U
      S_efficiency = E_budget * (1 - P_loop)

    Drifted Formula:
      E_budget_drifted = 1.0 - (U / B)
      P_loop_drifted = min(1.0, 0.20 * R)
    """
    # 1. At U = 1, B = 40 (single call used):
    # Canonical: E_budget = 1.0, P_loop = 0.0 -> Score = 1.0
    # Drifted: 1.0 - 1/40 = 0.975 (differs by 0.025)
    canonical_u1 = score_efficiency(tool_calls_count=1, duplicate_calls_count=0, budget=40, task_success=1.0)
    drifted_u1 = 1.0 - (1.0 / 40.0)
    assert canonical_u1 == 1.0
    assert canonical_u1 != drifted_u1

    # 2. At U = 10, R = 3, B = 40 (10 calls, 3 loops):
    # Canonical: E_budget = 1.0 - (10 - 1)/40 = 0.775; P_loop = 3/10 = 0.30; Score = 0.775 * 0.70 = 0.5425
    # Drifted: E_budget = 1.0 - 10/40 = 0.75; P_loop = min(1.0, 0.20 * 3) = 0.60; Score = 0.75 * 0.40 = 0.3000
    canonical_u10_r3 = score_efficiency(tool_calls_count=10, duplicate_calls_count=3, budget=40, task_success=1.0)
    drifted_u10_r3 = (1.0 - 10.0 / 40.0) * (1.0 - min(1.0, 0.20 * 3))
    assert canonical_u10_r3 == pytest.approx(0.5425, abs=1e-4)
    assert canonical_u10_r3 != pytest.approx(drifted_u10_r3, abs=1e-4)

    # 3. At U = 5, R = 1, B = 40:
    # Canonical: E_budget = 1.0 - (5 - 1)/40 = 0.90; P_loop = 1/5 = 0.20; Score = 0.90 * 0.80 = 0.720
    # Drifted: E_budget = 1.0 - 5/40 = 0.875; P_loop = 0.20; Score = 0.875 * 0.80 = 0.700
    canonical_u5_r1 = score_efficiency(tool_calls_count=5, duplicate_calls_count=1, budget=40, task_success=1.0)
    drifted_u5_r1 = (1.0 - 5.0 / 40.0) * 0.80
    assert canonical_u5_r1 == pytest.approx(0.720, abs=1e-4)
    assert canonical_u5_r1 != pytest.approx(drifted_u5_r1, abs=1e-4)

    # 4. At U = 0 (no tool calls):
    assert score_efficiency(tool_calls_count=0, duplicate_calls_count=0, budget=40, task_success=1.0) == 1.0
    assert score_efficiency(tool_calls_count=0, duplicate_calls_count=0, budget=40, task_success=0.0) == 0.0

    # 5. Over budget (U > B, e.g. U = 45, B = 40):
    assert score_efficiency(tool_calls_count=45, duplicate_calls_count=0, budget=40, task_success=1.0) == 0.0


def test_golden_communication_rubric_distinguishes_drifted_rubric():
    """Explicitly proves the 4 canonical communication criteria:
      1. Structure & Length (20-5000 chars): 0.25
      2. Clarity & Grounding (domain context keywords or case/doc/txn entity IDs): 0.25
      3. Absence of Unsupported Claims (no unaccomplished refund/cancellation promises): 0.25
      4. Decision Consistency (message communicates outcome consistent with resolution): 0.25

    Distinguishes from the drifted subjective rubric ("rationale / next steps / tone").
    """
    # Test 1: Length bounds (< 20 chars fails Structure & Length criterion)
    too_short = "Refund processed."  # 17 chars
    # Length: 0.0, Grounding (has 'refund'): 0.25, No Unsupported: 0.25, Consistency: 0.25 -> 0.75
    assert score_communication(too_short, "refund", 1.0, "refund") == 0.75

    too_long = "Your refund for transaction TXN-101 has been approved. " + ("A" * 5000)
    # Length: >5000 chars -> 0.0 for length
    assert score_communication(too_long, "refund", 1.0, "refund") == 0.75

    # Test 2: Clarity & Grounding requirement
    # A polite message with "next steps" and "professional tone" (drifted rubric) but NO domain keyword or entity ID:
    polite_ungrounded = (
        "Thank you for contacting us today. We have thoroughly reviewed everything and took care of the matter. "
        "Next steps are to monitor your email for updates. Have a nice day."
    )  # 182 chars, meets length (0.25), no unsupported claims (0.25), but fails grounding (0.0) and consistency (0.0)
    assert score_communication(polite_ungrounded, "refund", 1.0, "refund") == 0.50

    # Grounded with entity ID:
    grounded_entity = "Thank you for contacting us today. Your request regarding transaction TXN-101 has been resolved."
    # Length (0.25), Grounding (0.25 via TXN-101), No Unsupported (0.25), but no refund mention for refund consistency (0.0) -> 0.75
    assert score_communication(grounded_entity, "refund", 1.0, "refund") == 0.75

    # Grounded and fully consistent:
    grounded_full = "Thank you for contacting us today. Your refund for transaction TXN-101 has been processed."
    # Length (0.25) + Grounding (0.25) + No Unsupported (0.25) + Consistency (0.25) = 1.0
    assert score_communication(grounded_full, "refund", 1.0, "refund") == 1.0

    # Test 3: Absence of Unsupported Claims
    # Agent claims refund was processed, but submitted 'deny' or task_success == 0.0:
    unsupported_claim_msg = (
        "We have reviewed your inquiry and your refund for transaction TXN-101 has been successfully processed."
    )
    # When resolution is deny: Length (0.25) + Grounding (0.25) + Unsupported claim (0.0) + Consistency (0.0) = 0.50
    assert score_communication(unsupported_claim_msg, "deny", 0.0, "deny") == 0.50

    # When resolution is deny and response honestly communicates denial:
    honest_denial_msg = (
        "We have reviewed transaction TXN-101 and unfortunately cannot issue a refund per company policy."
    )
    # Length (0.25) + Grounding (0.25) + No Unsupported Claims (0.25) + Consistency (0.25) = 1.0
    assert score_communication(honest_denial_msg, "deny", 1.0, "deny") == 1.0


# ============================================================================
# Zero Trust Audit — Additional Gap Coverage Tests
# ============================================================================


def test_score_task_success_request_verification():
    """Proves request_verification action is scored correctly: checks verification_requests state."""
    gt = {
        "expected_action": {
            "tool": "request_verification",
            "params": {"customer_id": "CUS-001"},
        }
    }
    state_with_verif = {"verification_requests": [{"customer_id": "CUS-001", "verification_type": "identity"}]}
    state_empty_verif = {"verification_requests": []}
    state_no_verif_key = {}

    assert score_task_success("account_lock_fraud", "normal", state_with_verif, gt) == 1.0
    assert score_task_success("account_lock_fraud", "normal", state_empty_verif, gt) == 0.0
    assert score_task_success("account_lock_fraud", "normal", state_no_verif_key, gt) == 0.0


def test_score_task_success_escalation_with_specific_case():
    """Proves escalation scoring checks for the specific case_id when params include one."""
    gt = {
        "expected_action": {
            "tool": "escalate_case",
            "params": {"case_id": "CASE-XYZ"},
        }
    }
    state_correct_case = {"escalations": [{"case_id": "CASE-XYZ", "team": "billing"}]}
    state_wrong_case = {"escalations": [{"case_id": "CASE-OTHER", "team": "billing"}]}

    assert score_task_success("refund_request", "adversarial", state_correct_case, gt) == 1.0
    assert score_task_success("refund_request", "adversarial", state_wrong_case, gt) == 0.0


def test_score_evidence_empty_required_with_citations():
    """Proves that citing evidence when required is empty yields F1=0 (not F1=1).
    Mathematically correct: precision=0 kills F1 when TP=0 despite recall=1.0."""
    f1, prec, rec, tp = score_evidence(
        submitted_evidence=["DOC-001"],
        required_evidence=[],
        observed_evidence={"DOC-001"},
    )
    assert prec == 0.0  # 0 TP / 1 cited = 0
    assert rec == 1.0  # no requirements -> recall 1.0
    assert f1 == 0.0  # harmonic mean is 0 when precision=0


def test_score_evidence_completely_empty():
    """Proves that empty submitted, empty required, empty observed yields F1=1.0."""
    f1, prec, rec, tp = score_evidence([], [], set())
    assert f1 == 1.0
    assert prec == 1.0
    assert rec == 1.0


def test_score_communication_deny_resolution():
    """Proves deny resolution consistency scoring checks for cannot/unable/policy keywords."""
    deny_resp = (
        "Hello, we are unable to process your request as it does not meet our policy criteria. Sincerely, Support."
    )
    assert score_communication(deny_resp, "deny", 1.0, "deny") == 1.0

    # Deny response with a refund promise -> unsupported claim criterion fails
    deny_refund = "Hello, we cannot process your request. Your refund has been issued. Sincerely."
    assert score_communication(deny_refund, "deny", 1.0, "deny") < 1.0


def test_score_communication_escalate_resolution():
    """Proves escalate resolution consistency checks for escalat/specialist/team/review keywords."""
    esc_resp = "We are escalating your case to a specialist team for urgent review. Thank you."
    assert score_communication(esc_resp, "escalate", 1.0, "escalate") == 1.0


def test_score_policy_escalate_and_request_info_truthfulness():
    """Proves policy truthfulness checks for escalate and request_info resolution types."""
    state_with_esc = {"escalations": [{"case_id": "CASE-101"}]}
    state_no_esc = {"escalations": []}
    assert score_policy(0, "escalate", state_with_esc) == 1.0
    assert score_policy(0, "escalate", state_no_esc) == 0.0

    state_with_req = {"verification_requests": [{"customer_id": "CUS-001"}]}
    state_no_req = {"verification_requests": []}
    assert score_policy(0, "request_info", state_with_req) == 1.0
    assert score_policy(0, "request_info", state_no_req) == 0.0


def test_score_robustness_passthrough_all_variants():
    """Proves robustness is a pure passthrough of task_success for every variant."""
    for variant in ["normal", "distractor", "contradiction", "missing_info", "adversarial", "stale"]:
        assert score_robustness(variant, 1.0) == 1.0
        assert score_robustness(variant, 0.0) == 0.0


def test_score_efficiency_boundary_values():
    """Proves efficiency boundary values at exactly-budget and budget+1 calls."""
    budget = 40
    # 1 call: maximum efficiency 1.0
    assert score_efficiency(1, 0, budget, 1.0) == 1.0
    # Exactly at budget: 1.0 - (budget-1)/budget
    e_at = score_efficiency(budget, 0, budget, 1.0)
    assert e_at == pytest.approx(1.0 - (budget - 1) / float(budget), abs=1e-4)
    # One over budget: 0.0
    assert score_efficiency(budget + 1, 0, budget, 1.0) == 0.0
    # 0 calls, success: 1.0 (agent solved with no tool usage)
    assert score_efficiency(0, 0, budget, 1.0) == 1.0
    # 0 calls, failure: 0.0
    assert score_efficiency(0, 0, budget, 0.0) == 0.0
