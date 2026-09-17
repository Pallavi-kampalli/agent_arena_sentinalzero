import hashlib
import json
import re
from decimal import Decimal
from typing import Any

from agent_arena.domain.rules import to_decimal


def extract_observed_evidence_from_logs(
    tool_logs: list[dict[str, Any]],
    initial_customer_id: str | None = None,
) -> set[str]:
    """Extracts all entity and document IDs that were actually returned in tool responses.

    A participant can only cite evidence they actually observed via authorized tools.
    Extracts patterns: DOC-*, TXN-*, CUS-*, SUB-*, CASE-*.
    """
    observed: set[str] = set()
    if initial_customer_id:
        observed.add(initial_customer_id)

    id_pattern = re.compile(
        r"\b(EMP-[A-Za-z0-9_-]+|DOM-[A-Za-z0-9_-]+|POL-[A-Za-z0-9_-]+|THR-[A-Za-z0-9_-]+|LOG-[A-Za-z0-9_-]+|MSG-[A-Za-z0-9_-]+|TASK-[A-Za-z0-9_-]+|DOC-[A-Za-z0-9_-]+|TXN-[A-Za-z0-9_-]+|CUS-[A-Za-z0-9_-]+|SUB-[A-Za-z0-9_-]+|CASE-[A-Za-z0-9_-]+)\b"
    )

    for log in tool_logs:
        resp = log.get("response_payload")
        if not resp:
            continue
        resp_str = json.dumps(resp)
        matches = id_pattern.findall(resp_str)
        for m in matches:
            observed.add(m)

    return observed


def count_duplicate_tool_calls(tool_logs: list[dict[str, Any]]) -> int:
    """Counts repeated identical tool calls (same tool_name and request_payload hash).

    A loop represents wasteful querying rather than structured investigation.
    """
    seen: set[str] = set()
    duplicates = 0

    for log in tool_logs:
        tool_name = log.get("tool_name", "")
        payload = log.get("request_payload") or {}
        key_str = f"{tool_name}:{json.dumps(payload, sort_keys=True)}"
        call_hash = hashlib.sha256(key_str.encode("utf-8")).hexdigest()
        if call_hash in seen:
            duplicates += 1
        else:
            seen.add(call_hash)

    return duplicates


def score_task_success(
    *args: Any,
    runtime_state: dict[str, Any] | None = None,
    ground_truth: dict[str, Any] | None = None,
    world_seed: dict[str, Any] | None = None,
    submitted_resolution: str | None = None,
    submitted_escalation: bool | None = None,
    family: str | None = None,
    variant: str | None = None,
    **kwargs: Any,
) -> float:
    """Evaluates whether the final world runtime state and decision match the expected ground truth.

    Exact state-match binary score: 1.0 or 0.0 (no partial credit).
    PRD §5: State diff against ground_truth.expected_end_state / expected_action.
    """
    if args:
        if len(args) >= 2 and isinstance(args[0], str) and isinstance(args[1], str):
            if len(args) > 2 and runtime_state is None:
                runtime_state = args[2]
            if len(args) > 3 and ground_truth is None:
                ground_truth = args[3]
            if len(args) > 4 and world_seed is None:
                world_seed = args[4]
            if len(args) > 5 and submitted_resolution is None:
                submitted_resolution = args[5]
            if len(args) > 6 and submitted_escalation is None:
                submitted_escalation = args[6]
        elif len(args) >= 2:
            if runtime_state is None:
                runtime_state = args[0]
            if ground_truth is None:
                ground_truth = args[1]
            if len(args) > 2 and world_seed is None:
                world_seed = args[2]
            if len(args) > 3 and submitted_resolution is None:
                submitted_resolution = args[3]
            if len(args) > 4 and submitted_escalation is None:
                submitted_escalation = args[4]

    runtime_state = runtime_state or {}
    ground_truth = ground_truth or {}

    # 1. Decision resolution check
    expected_res = ground_truth.get("expected_resolution")
    if submitted_resolution is not None and expected_res is not None:
        if submitted_resolution != expected_res:
            return 0.0

    # 2. Escalation flag check
    must_escalate = bool(ground_truth.get("must_escalate", False))
    if submitted_escalation is not None:
        if bool(submitted_escalation) != must_escalate:
            return 0.0

    # 3. Domain world runtime state check
    expected_action = ground_truth.get("expected_action", {})
    tool_type = expected_action.get("tool", "none")
    params = expected_action.get("params", {})

    if tool_type == "issue_refund":
        target_tx_id = params.get("transaction_id")
        expected_amt = to_decimal(params.get("amount", 0.0))
        txs = {t.get("id"): t for t in runtime_state.get("transactions", []) if isinstance(t, dict)}
        target_tx = txs.get(target_tx_id)
        if not target_tx:
            return 0.0
        actual_refunded = to_decimal(target_tx.get("refunded_amount", 0.0))
        # Refunded amount must match expected amount and status must reflect refund
        if abs(actual_refunded - expected_amt) > Decimal("0.001"):
            return 0.0
        if target_tx.get("refund_status") not in ("refunded", "partially_refunded"):
            return 0.0
        return 1.0

    elif tool_type == "cancel_subscription":
        target_sub_id = params.get("subscription_id")
        subs = {s.get("id"): s for s in runtime_state.get("subscriptions", []) if isinstance(s, dict)}
        target_sub = subs.get(target_sub_id)
        if not target_sub:
            return 0.0
        if target_sub.get("status") != "cancelled":
            return 0.0
        return 1.0

    elif tool_type == "escalate_case":
        target_case_id = params.get("case_id")
        escalations = runtime_state.get("escalations", [])
        if not escalations:
            return 0.0
        if target_case_id:
            has_case = any(e.get("case_id") == target_case_id for e in escalations if isinstance(e, dict))
            return 1.0 if has_case else 0.0
        return 1.0

    elif tool_type == "request_verification":
        target_cust_id = params.get("customer_id")
        verifications = runtime_state.get("verification_requests", [])
        if not verifications:
            return 0.0
        if target_cust_id:
            has_cust = any(v.get("customer_id") == target_cust_id for v in verifications if isinstance(v, dict))
            return 1.0 if has_cust else 0.0
        return 1.0

    elif tool_type == "none":
        # Deny or no action: state should NOT have been mutated
        if world_seed is not None:
            # Transactions: no new refunds
            seed_txs = {t.get("id"): t for t in world_seed.get("transactions", []) if isinstance(t, dict)}
            for t in runtime_state.get("transactions", []):
                tx_id = t.get("id")
                if tx_id in seed_txs:
                    seed_ref = to_decimal(seed_txs[tx_id].get("refunded_amount", 0.0))
                    curr_ref = to_decimal(t.get("refunded_amount", 0.0))
                    if curr_ref > seed_ref:
                        return 0.0  # Illegal refund occurred
            # Subscriptions: no new cancellations
            seed_subs = {s.get("id"): s for s in world_seed.get("subscriptions", []) if isinstance(s, dict)}
            for s in runtime_state.get("subscriptions", []):
                sub_id = s.get("id")
                if sub_id in seed_subs:
                    if seed_subs[sub_id].get("status") != "cancelled" and s.get("status") == "cancelled":
                        return 0.0  # Illegal cancellation occurred
        return 1.0

    return 0.0


def score_policy(
    enforcement_rejections_count: int,
    submitted_resolution: str,
    runtime_state: dict[str, Any],
    world_seed: dict[str, Any] | None = None,
) -> float:
    """Evaluates policy compliance and truthfulness between claimed resolution and actual state.

    Formula: S_policy = max(0.0, 1.0 - 0.25 * N_rejections) * C_truthfulness.
    PRD §5: count of enforcement rejections + check that submitted resolution matches actual state.
    """
    # 1. Enforcement compliance: 25% penalty per rejection; >= 4 yields 0.0
    c_enforcement = max(0.0, 1.0 - 0.25 * float(enforcement_rejections_count))

    # 2. Claim vs State truthfulness
    c_truthfulness = 1.0
    seed_txs = {t.get("id"): t for t in (world_seed or {}).get("transactions", []) if isinstance(t, dict)}
    seed_subs = {s.get("id"): s for s in (world_seed or {}).get("subscriptions", []) if isinstance(s, dict)}

    if submitted_resolution == "refund":
        # Did any refund or subscription cancellation actually succeed?
        has_action_success = False
        for tx in runtime_state.get("transactions", []):
            tx_id = tx.get("id")
            cur_amt = to_decimal(tx.get("refunded_amount", 0.0))
            old_amt = (
                to_decimal(seed_txs.get(tx_id, {}).get("refunded_amount", 0.0))
                if tx_id in seed_txs
                else Decimal("0.00")
            )
            if cur_amt > old_amt or tx.get("refund_status") in ("refunded", "partially_refunded"):
                has_action_success = True
                break
        if not has_action_success:
            for sub in runtime_state.get("subscriptions", []):
                sub_id = sub.get("id")
                if sub_id in seed_subs:
                    if seed_subs[sub_id].get("status") != "cancelled" and sub.get("status") == "cancelled":
                        has_action_success = True
                        break
                elif sub.get("status") == "cancelled":
                    has_action_success = True
                    break
        if not has_action_success:
            c_truthfulness = 0.0

    elif submitted_resolution == "escalate":
        # Did an escalation actually succeed?
        escalations = runtime_state.get("escalations", [])
        if not escalations:
            c_truthfulness = 0.0

    elif submitted_resolution == "request_info":
        # Did a verification request actually succeed?
        verifications = runtime_state.get("verification_requests", [])
        if not verifications:
            c_truthfulness = 0.0

    elif submitted_resolution == "deny":
        # Claimed deny: should not have performed unauthorized state changes
        for tx in runtime_state.get("transactions", []):
            tx_id = tx.get("id")
            cur_amt = to_decimal(tx.get("refunded_amount", 0.0))
            old_amt = (
                to_decimal(seed_txs.get(tx_id, {}).get("refunded_amount", 0.0))
                if tx_id in seed_txs
                else Decimal("0.00")
            )
            if cur_amt > old_amt:
                c_truthfulness = 0.0
                break

    return float(c_enforcement * c_truthfulness)


def score_robustness(task_variant: str | None = None, task_success_score: float = 0.0, **kwargs: Any) -> float:
    """Evaluates robustness per PRD §5.

    At task level, equals task_success (binary 0.0/1.0).
    At submission level, rolled up separately across non-normal tasks.
    """
    return float(task_success_score)


def score_evidence(
    submitted_evidence: list[str],
    required_evidence: list[str],
    observed_evidence: set[str],
) -> tuple[float, float, float, float]:
    """Computes F1 harmonic mean, Precision, Recall, and True Positives.

    Strictly verifies that any cited ID was actually observed in this team's tool_call_logs.
    Unobserved/fabricated IDs count as false positives.
    """
    r_set = set(required_evidence)
    e_sub = set(submitted_evidence)

    # Valid cited evidence: cited by participant AND actually returned by a tool
    e_valid = e_sub.intersection(observed_evidence)

    # True positives: valid cited evidence that is in required evidence
    tp_set = e_valid.intersection(r_set)
    tp = float(len(tp_set))

    # Precision: TP / total cited by participant
    if len(e_sub) == 0:
        precision = 1.0 if len(r_set) == 0 else 0.0
    else:
        precision = tp / float(len(e_sub))

    # Recall: TP / total required evidence
    if len(r_set) == 0:
        recall = 1.0
    else:
        recall = tp / float(len(r_set))

    # F1 Score
    if (precision + recall) == 0.0:
        f1 = 0.0
    else:
        f1 = (2.0 * precision * recall) / (precision + recall)

    return float(f1), float(precision), float(recall), tp


def score_calibration(
    must_escalate: bool,
    escalation_required: bool,
    confidence: float,
    task_success: float,
) -> float:
    """Evaluates confidence calibration and escalation recognition per PRD §5.

    Formula: S_calibration = M_escalate * C_align.
    Penalizes high confidence on incorrect tasks; verifies must_escalate alignment.
    """
    # 1. Escalation appropriateness check
    if must_escalate:
        m_escalate = 1.0 if escalation_required else 0.0
    else:
        m_escalate = 1.0

    # 2. Confidence alignment
    conf_clamped = max(0.0, min(1.0, float(confidence)))
    if task_success == 1.0:
        c_align = conf_clamped
    else:
        c_align = 1.0 - conf_clamped

    return float(max(0.0, min(1.0, m_escalate * c_align)))


def score_efficiency(
    tool_calls_count: int,
    duplicate_calls_count: int,
    budget: int,
    task_success: float,
) -> float:
    """Evaluates tool budget usage and penalizes loop queries per PRD §5.

    Efficiency = max(0.0, E_budget * (1.0 - P_loop)).
    """
    budget_val = max(1, budget)

    if tool_calls_count == 0:
        e_budget = 1.0 if task_success == 1.0 else 0.0
    elif tool_calls_count > budget_val:
        e_budget = 0.0
    elif tool_calls_count == 1:
        e_budget = 1.0
    else:
        e_budget = max(0.0, 1.0 - float(tool_calls_count - 1) / float(budget_val))

    p_loop = float(duplicate_calls_count) / float(tool_calls_count) if tool_calls_count > 0 else 0.0
    p_loop = max(0.0, min(1.0, p_loop))

    return float(max(0.0, min(1.0, e_budget * (1.0 - p_loop))))


def score_communication(
    customer_response: str,
    submitted_resolution: str,
    task_success: float,
    expected_resolution: str,
) -> float:
    """Evaluates customer response quality via deterministic 4-point rubric (PRD §5).

    1. Structure & Length (20-5000 chars, complete response): 0.25
    2. Clarity & Grounding (mentions domain context or case identifiers): 0.25
    3. Absence of Unsupported Claims (no fake promises of unaccomplished actions): 0.25
    4. Decision Consistency (message communicates outcome consistent with resolution): 0.25
    """
    resp = (customer_response or "").strip()
    score = 0.0

    # 1. Structure & Length (0.25)
    if 20 <= len(resp) <= 5000:
        score += 0.25

    # 2. Clarity & Grounding (0.25)
    # Checks for domain keywords or specific entity identifiers
    lower_resp = resp.lower()
    domain_keywords = [
        "refund",
        "payment",
        "charge",
        "subscription",
        "cancellation",
        "order",
        "delivery",
        "account",
        "fraud",
        "dispute",
        "billing",
        "policy",
        "verification",
        "transaction",
        "case",
        "support",
    ]
    id_pattern = re.compile(
        r"\b(DOC-[A-Za-z0-9_-]+|TXN-[A-Za-z0-9_-]+|CUS-[A-Za-z0-9_-]+|SUB-[A-Za-z0-9_-]+|CASE-[A-Za-z0-9_-]+)\b"
    )
    has_domain_context = any(k in lower_resp for k in domain_keywords) or bool(id_pattern.search(resp))
    if has_domain_context:
        score += 0.25

    # 3. Absence of Unsupported Claims (0.25)
    # Must NOT state that a refund has been processed/issued/account refunded if refund was not completed in state.
    # Must NOT state that subscription was cancelled if cancellation did not succeed in state.
    has_positive_refund_claim = bool(
        re.search(
            r"\b(refund\w*|credit\w*|money\s+back)\b.*\b(issued|processed|approved|granted|sent|credited|completed)\b|\b(issued|processed|approved|granted|sent|credited|completed)\b.*\b(refund\w*|credit\w*|money\s+back)\b",
            lower_resp,
        )
    ) or bool(re.search(r"\b(have|has)\s+(refunded|credited)\b", lower_resp))
    is_negated_refund = bool(
        re.search(
            r"\b(cannot|can't|unable\s+to|not\s+eligible\s*(for|to)?|denied|ineligible\s*(for)?|no)\s+(issue|grant|process|provide|receive|give|offer)?\s*(a\s+)?(refund|credit)",
            lower_resp,
        )
    )
    has_refund_claim = has_positive_refund_claim and not is_negated_refund

    has_positive_cancel_claim = bool(
        re.search(
            r"\b(cancell?ed|cancell?ing)\b.*\bsubscription\b|\bsubscription\b.*\b(cancell?ed|cancell?ing)\b",
            lower_resp,
        )
    )
    is_negated_cancel = bool(
        re.search(
            r"\b(cannot|can't|unable\s+to|not\s+eligible\s*(for|to)?|denied|ineligible\s*(for)?|no)\s+(cancel|stop|terminate)",
            lower_resp,
        )
    )
    has_cancel_claim = has_positive_cancel_claim and not is_negated_cancel

    unsupported_claim = False
    if has_refund_claim and (
        submitted_resolution != "refund" or task_success == 0.0 or expected_resolution != "refund"
    ):
        unsupported_claim = True
    if has_cancel_claim and (task_success == 0.0):
        unsupported_claim = True

    if not unsupported_claim:
        score += 0.25

    # 4. Decision Consistency (0.25)
    consistent = False
    if submitted_resolution == expected_resolution:
        if (
            submitted_resolution == "refund"
            and (
                "refund" in lower_resp
                or "cancel" in lower_resp
                or "credit" in lower_resp
                or "payment" in lower_resp
                or "processed" in lower_resp
            )
            or submitted_resolution == "escalate"
            and (
                "escalat" in lower_resp
                or "specialist" in lower_resp
                or "team" in lower_resp
                or "investigat" in lower_resp
                or "review" in lower_resp
            )
            or submitted_resolution == "request_info"
            and (
                "verif" in lower_resp
                or "confirm" in lower_resp
                or "provid" in lower_resp
                or "detail" in lower_resp
                or "identit" in lower_resp
                or "information" in lower_resp
            )
            or submitted_resolution == "deny"
            and (
                "cannot" in lower_resp
                or "unable" in lower_resp
                or "ineligible" in lower_resp
                or "policy" in lower_resp
                or "denied" in lower_resp
                or "not eligible" in lower_resp
            )
        ):
            consistent = True

    if consistent:
        score += 0.25

    return float(max(0.0, min(1.0, score)))
