import copy
from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from agent_arena.domain.models import EligibilityResult

CENT = Decimal("0.01")


def to_decimal(val: Any) -> Decimal:
    """Converts numeric or string value to 2-decimal Decimal using standard half-up rounding."""
    if isinstance(val, Decimal):
        return val.quantize(CENT, rounding=ROUND_HALF_UP)
    return Decimal(str(val if val is not None else 0)).quantize(CENT, rounding=ROUND_HALF_UP)


def parse_iso(dt_str: str) -> datetime:
    """Parses ISO timestamp string to timezone-aware UTC datetime."""
    try:
        dt = datetime.fromisoformat(dt_str.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return dt.astimezone(UTC)
    except Exception:
        return datetime(2026, 1, 1, tzinfo=UTC)


def get_authoritative_policy(world_state: dict[str, Any], category: str) -> dict[str, Any] | None:
    """Returns the latest authoritative policy document for a given category.

    If multiple documents exist (e.g. current vs stale/superseded), selects the one
    with the latest updated_at timestamp.
    """
    policies = world_state.get("policies", [])
    candidates = [p for p in policies if isinstance(p, dict) and p.get("category") == category]
    if not candidates:
        # Check general documents as fallback
        docs = world_state.get("documents", [])
        candidates = [d for d in docs if isinstance(d, dict) and d.get("category") == category]
    if not candidates:
        return None

    # Sort by updated_at descending
    candidates.sort(key=lambda p: parse_iso(p.get("updated_at", "1970-01-01T00:00:00Z")), reverse=True)
    return candidates[0]


def check_refund_eligibility(
    world_state: dict[str, Any],
    transaction_id: str,
    amount: float,
    reason: str,
) -> EligibilityResult:
    """Canonical refund eligibility check per SupportOps_PS_v2.md §5.2.

    Checks:
    1. Transaction existence and validity
    2. Not already refunded or refund amount doesn't exceed original charge
    3. No active chargeback or fraud investigation (DOC-1842 §4)
    4. Within refund policy time window (e.g. 30 days) from authoritative policy
    5. Amount within limits
    """
    transactions = {t.get("id"): t for t in world_state.get("transactions", []) if isinstance(t, dict) and "id" in t}
    tx = transactions.get(transaction_id)
    if not tx:
        return EligibilityResult(
            is_eligible=False,
            error="TRANSACTION_NOT_FOUND",
            reason=f"Transaction '{transaction_id}' does not exist in account records.",
        )

    refund_policy = get_authoritative_policy(world_state, "refund")
    policy_doc_id = refund_policy.get("id", "DOC-1001") if refund_policy else "DOC-1001"

    # 1. Check already refunded or amount exceedance (exact Decimal arithmetic)
    already_refunded = tx.get("refund_status") == "refunded"
    total_refunded = to_decimal(tx.get("refunded_amount", 0.0))
    tx_amount = to_decimal(tx.get("amount", 0.0))
    amount_to_refund = to_decimal(amount)

    if already_refunded or (total_refunded >= tx_amount):
        return EligibilityResult(
            is_eligible=False,
            error="INELIGIBLE",
            reason="already_refunded",
            policy_ref=policy_doc_id,
        )

    if (total_refunded + amount_to_refund) > tx_amount:
        return EligibilityResult(
            is_eligible=False,
            error="INELIGIBLE",
            reason="amount_exceeds_transaction",
            policy_ref=policy_doc_id,
        )

    # 2. Check active chargeback / fraud hold (Working example from SupportOps_PS_v2.md §5.2)
    if tx.get("chargeback_status") == "investigation_active" or tx.get("under_fraud_investigation", False):
        hold_policy = get_authoritative_policy(world_state, "dispute_hold")
        hold_doc_id = hold_policy.get("id", "DOC-1842") if hold_policy else "DOC-1842"
        return EligibilityResult(
            is_eligible=False,
            error="INELIGIBLE",
            reason="chargeback_investigation_active",
            policy_ref=hold_doc_id,
        )

    # 3. Check refund window against authoritative policy
    max_days = refund_policy.get("rules", {}).get("refund_window_days", 30) if refund_policy else 30

    current_date_str = world_state.get("current_date", "2026-09-15T00:00:00Z")
    current_dt = parse_iso(current_date_str)
    tx_dt = parse_iso(tx.get("date", current_date_str))
    days_diff = (current_dt - tx_dt).days

    if days_diff > max_days:
        return EligibilityResult(
            is_eligible=False,
            error="INELIGIBLE",
            reason="outside_refund_window",
            policy_ref=policy_doc_id,
        )

    # All checks pass
    return EligibilityResult(
        is_eligible=True,
        status="refunded",
    )


def check_cancellation_eligibility(
    world_state: dict[str, Any],
    customer_id: str,
    subscription_id: str,
) -> EligibilityResult:
    """Canonical cancellation eligibility check per SupportOps_PS_v2.md §5.3.

    Checks:
    1. Subscription existence and ownership
    2. Active subscription status
    3. Contractual lock-in period (requires approved exception to cancel early)
    4. Unresolved billing dispute blocking cancellation
    """
    subscriptions = {s.get("id"): s for s in world_state.get("subscriptions", []) if isinstance(s, dict) and "id" in s}
    sub = subscriptions.get(subscription_id)
    if not sub:
        return EligibilityResult(
            is_eligible=False,
            error="SUBSCRIPTION_NOT_FOUND",
            reason=f"Subscription '{subscription_id}' does not exist.",
        )

    if sub.get("customer_id") != customer_id:
        return EligibilityResult(
            is_eligible=False,
            error="INELIGIBLE",
            reason="subscription_customer_mismatch",
        )

    cancel_policy = get_authoritative_policy(world_state, "cancellation")
    policy_doc_id = cancel_policy.get("id", "DOC-1003") if cancel_policy else "DOC-1003"

    if sub.get("status") == "cancelled":
        return EligibilityResult(
            is_eligible=False,
            error="INELIGIBLE",
            reason="subscription_already_cancelled",
            policy_ref=policy_doc_id,
        )

    # Check unresolved dispute
    if sub.get("has_unresolved_dispute", False):
        return EligibilityResult(
            is_eligible=False,
            error="INELIGIBLE",
            reason="unresolved_billing_dispute",
            policy_ref=policy_doc_id,
        )

    # Check contractual lock-in
    lock_in_until = sub.get("lock_in_until")
    current_date_str = world_state.get("current_date", "2026-09-15T00:00:00Z")
    if lock_in_until:
        lock_in_dt = parse_iso(lock_in_until)
        current_dt = parse_iso(current_date_str)
        if lock_in_dt > current_dt and not sub.get("has_approved_exception", False):
            return EligibilityResult(
                is_eligible=False,
                error="INELIGIBLE",
                reason="lock_in_period_active",
                policy_ref=policy_doc_id,
            )

    return EligibilityResult(
        is_eligible=True,
        status="cancelled",
    )


def check_escalation_validity(
    world_state: dict[str, Any],
    case_id: str,
    team: str,
    reason: str,
    retrieved_evidence_ids: set[str] | list[str] | None = None,
) -> EligibilityResult:
    """Canonical escalation check per SupportOps_PS_v2.md §4.2, §5.3.

    Must include a reason grounded in something retrievable (a policy ref or evidence ID).
    Empty or generic reasons ('customer mad', 'need help') are rejected.
    Keyword mentions ('fraud', 'chargeback') without citing retrievable evidence are strictly rejected.
    """
    if not reason or len(reason.strip()) < 5:
        return EligibilityResult(
            is_eligible=False,
            error="INVALID_ESCALATION",
            reason="reason_not_grounded",
        )

    # Known retrievable IDs in the world state
    retrievable_ids = set()
    for doc in world_state.get("policies", []):
        if isinstance(doc, dict) and doc.get("id"):
            retrievable_ids.add(doc.get("id"))
    for doc in world_state.get("documents", []):
        if isinstance(doc, dict) and doc.get("id"):
            retrievable_ids.add(doc.get("id"))
    for tx in world_state.get("transactions", []):
        if isinstance(tx, dict) and tx.get("id"):
            retrievable_ids.add(tx.get("id"))
    for cs in world_state.get("historical_cases", []):
        if isinstance(cs, dict) and cs.get("case_id"):
            retrievable_ids.add(cs.get("case_id"))
    for sub in world_state.get("subscriptions", []):
        if isinstance(sub, dict) and sub.get("id"):
            retrievable_ids.add(sub.get("id"))
    for cust in world_state.get("customers", []):
        if isinstance(cust, dict) and cust.get("id"):
            retrievable_ids.add(cust.get("id"))

    # If retrieved_evidence_ids is provided (runtime checking in Phase 2/scoring),
    # verify the reason cites an ID that was actually retrieved by the team.
    candidate_ids = set(retrieved_evidence_ids) if retrieved_evidence_ids is not None else retrievable_ids

    # Reason must cite at least one candidate evidence ID
    has_grounded_ref = any(eid in reason for eid in candidate_ids if eid)

    if not has_grounded_ref:
        return EligibilityResult(
            is_eligible=False,
            error="INVALID_ESCALATION",
            reason="reason_not_grounded",
        )

    return EligibilityResult(
        is_eligible=True,
        status="escalated",
    )


def apply_action_to_world(
    world_state: dict[str, Any],
    action_type: str,
    params: dict[str, Any],
    retrieved_evidence_ids: set[str] | list[str] | None = None,
) -> tuple[dict[str, Any], EligibilityResult]:
    """Applies an action to a working copy of world state.

    Returns (mutated_world_state, eligibility_result).
    If ineligible, world state remains completely unmodified (PS §5.1).
    """
    state_copy = copy.deepcopy(world_state)

    if action_type == "issue_refund":
        tx_id = params.get("transaction_id", "")
        amount_dec = to_decimal(params.get("amount", 0.0))
        reason = params.get("reason", "")
        result = check_refund_eligibility(state_copy, tx_id, float(amount_dec), reason)
        if result.is_eligible:
            for tx in state_copy.get("transactions", []):
                if isinstance(tx, dict) and tx.get("id") == tx_id:
                    current_refunded = to_decimal(tx.get("refunded_amount", 0.0))
                    tx_amt = to_decimal(tx.get("amount", 0.0))
                    new_refunded = current_refunded + amount_dec
                    tx["refunded_amount"] = float(new_refunded)
                    tx["refunded_at"] = state_copy.get("current_date", "2026-09-15T00:00:00Z")
                    if new_refunded >= tx_amt:
                        tx["refund_status"] = "refunded"
                    else:
                        tx["refund_status"] = "partially_refunded"
                    break
        return state_copy if result.is_eligible else world_state, result

    elif action_type == "cancel_subscription":
        cust_id = params.get("customer_id", "")
        sub_id = params.get("subscription_id", "")
        result = check_cancellation_eligibility(state_copy, cust_id, sub_id)
        if result.is_eligible:
            for sub in state_copy.get("subscriptions", []):
                if isinstance(sub, dict) and sub.get("id") == sub_id:
                    sub["status"] = "cancelled"
                    sub["cancelled_at"] = state_copy.get("current_date", "2026-09-15T00:00:00Z")
                    sub["auto_renew"] = False
                    break
        return state_copy if result.is_eligible else world_state, result

    elif action_type == "escalate_case":
        case_id = params.get("case_id", "")
        team = params.get("team", "")
        reason = params.get("reason", "")
        result = check_escalation_validity(state_copy, case_id, team, reason, retrieved_evidence_ids)
        if result.is_eligible:
            escalations = state_copy.setdefault("escalations", [])
            escalations.append(
                {
                    "case_id": case_id,
                    "team": team,
                    "reason": reason,
                    "timestamp": state_copy.get("current_date", "2026-09-15T00:00:00Z"),
                }
            )
        return state_copy if result.is_eligible else world_state, result

    elif action_type == "request_verification":
        # Always succeeds (PS §4.2, §5.3)
        cust_id = params.get("customer_id", "")
        vtype = params.get("verification_type", "identity")
        requests = state_copy.setdefault("verification_requests", [])
        requests.append(
            {
                "customer_id": cust_id,
                "verification_type": vtype,
                "timestamp": state_copy.get("current_date", "2026-09-15T00:00:00Z"),
            }
        )
        return state_copy, EligibilityResult(is_eligible=True, status="verification_requested")

    else:
        return world_state, EligibilityResult(is_eligible=True, status="no_action")
