from typing import Any

from agent_arena.domain.models import ActionType, GroundTruth, ResolutionType
from agent_arena.domain.rules import (
    apply_action_to_world,
    parse_iso,
)


class ReferenceSolver:
    """Canonical Reference Solver.

    Derives deterministic ground truth directly from the task's world state seed
    and task specification using the exact canonical domain rules (PRD §8).
    """

    @classmethod
    def solve(
        cls,
        world_state_seed: dict[str, Any],
        input_payload: dict[str, Any],
        family: str,
        variant: str,
        task_meta: dict[str, Any] | None = None,
    ) -> GroundTruth:
        meta = task_meta or {}
        cust_id = input_payload.get("customer_id", "")

        if family == "duplicate_payment":
            return cls._solve_duplicate_payment(world_state_seed, cust_id, variant, meta)
        elif family == "refund_request":
            return cls._solve_refund_request(world_state_seed, cust_id, variant, meta)
        elif family == "subscription_cancellation":
            return cls._solve_subscription_cancellation(world_state_seed, cust_id, variant, meta)
        elif family == "delivery_dispute":
            return cls._solve_delivery_dispute(world_state_seed, cust_id, variant, meta)
        elif family == "account_lock_fraud":
            return cls._solve_account_lock_fraud(world_state_seed, cust_id, variant, meta)
        elif family == "previous_agent_was_wrong":
            return cls._solve_previous_agent(world_state_seed, cust_id, variant, meta)
        else:
            raise ValueError(f"Unknown task family: {family}")

    @classmethod
    def _solve_duplicate_payment(
        cls,
        world: dict[str, Any],
        cust_id: str,
        variant: str,
        meta: dict[str, Any],
    ) -> GroundTruth:
        is_true_duplicate = meta.get("is_true_duplicate", True)
        primary_tx_id = meta.get("primary_tx_id", "")
        duplicate_tx_id = meta.get("duplicate_tx_id", "")
        amount = float(meta.get("amount", 99.0))

        if variant == "missing_info":
            # Cannot identify duplicate transaction safely without customer providing transaction or invoice details
            return GroundTruth(
                expected_resolution=ResolutionType.REQUEST_INFO,
                must_escalate=False,
                required_evidence=["DOC-1002"],
                expected_action={"tool": ActionType.REQUEST_VERIFICATION.value, "params": {"customer_id": cust_id}},
                expected_end_state=apply_action_to_world(world, "request_verification", {"customer_id": cust_id})[0],
                classification={"category": "billing", "issue": "duplicate_payment", "severity": "medium"},
            )

        if is_true_duplicate:
            # Action: refund the duplicate transaction
            action_params = {"transaction_id": duplicate_tx_id, "amount": amount, "reason": "duplicate payment refund"}
            end_state, _ = apply_action_to_world(world, "issue_refund", action_params)
            evidence = [primary_tx_id, duplicate_tx_id, "DOC-1002"]
            if meta.get("stale_doc_id") and variant == "stale":
                evidence.append("DOC-1002")

            return GroundTruth(
                expected_resolution=ResolutionType.REFUND,
                must_escalate=False,
                required_evidence=evidence,
                expected_action={"tool": ActionType.ISSUE_REFUND.value, "params": action_params},
                expected_end_state=end_state,
                classification={"category": "billing", "issue": "duplicate_payment", "severity": "medium"},
            )
        else:
            # Legitimate separate charges (e.g. different invoices)
            return GroundTruth(
                expected_resolution=ResolutionType.DENY,
                must_escalate=False,
                required_evidence=[primary_tx_id, duplicate_tx_id, "DOC-1002"],
                expected_action={"tool": ActionType.NONE.value, "params": {}},
                expected_end_state=world,
                classification={"category": "billing", "issue": "duplicate_payment", "severity": "low"},
            )

    @classmethod
    def _solve_refund_request(
        cls,
        world: dict[str, Any],
        cust_id: str,
        variant: str,
        meta: dict[str, Any],
    ) -> GroundTruth:
        tx_id = meta.get("transaction_id", "")
        amount = float(meta.get("amount", 99.0))
        tx = next((t for t in world.get("transactions", []) if t["id"] == tx_id), None)

        if variant == "missing_info" or not tx:
            return GroundTruth(
                expected_resolution=ResolutionType.REQUEST_INFO,
                must_escalate=False,
                required_evidence=["DOC-1001"],
                expected_action={"tool": ActionType.REQUEST_VERIFICATION.value, "params": {"customer_id": cust_id}},
                expected_end_state=apply_action_to_world(world, "request_verification", {"customer_id": cust_id})[0],
                classification={"category": "billing", "issue": "refund_request", "severity": "low"},
            )

        # Check chargeback hold (PS §5.2)
        if tx.get("chargeback_status") == "investigation_active" or tx.get("under_fraud_investigation"):
            action_params = {
                "case_id": meta.get("case_id", f"CASE-{tx_id}"),
                "team": "billing_specialists",
                "reason": f"active chargeback investigation blocks automatic refund on {tx_id} per DOC-1842",
            }
            end_state, _ = apply_action_to_world(world, "escalate_case", action_params)
            return GroundTruth(
                expected_resolution=ResolutionType.ESCALATE,
                must_escalate=True,
                required_evidence=[tx_id, "DOC-1842"],
                expected_action={"tool": ActionType.ESCALATE_CASE.value, "params": action_params},
                expected_end_state=end_state,
                classification={"category": "billing", "issue": "refund_request", "severity": "high"},
            )

        # Check refund window (30 days)
        current_dt = parse_iso(world.get("current_date", "2026-09-15T00:00:00Z"))
        tx_dt = parse_iso(tx.get("date", "2026-09-15T00:00:00Z"))
        if (current_dt - tx_dt).days > 30:
            return GroundTruth(
                expected_resolution=ResolutionType.DENY,
                must_escalate=False,
                required_evidence=[tx_id, "DOC-1001"],
                expected_action={"tool": ActionType.NONE.value, "params": {}},
                expected_end_state=world,
                classification={"category": "billing", "issue": "refund_request", "severity": "low"},
            )

        # Eligible refund
        action_params = {"transaction_id": tx_id, "amount": amount, "reason": "standard customer refund request"}
        end_state, _ = apply_action_to_world(world, "issue_refund", action_params)
        return GroundTruth(
            expected_resolution=ResolutionType.REFUND,
            must_escalate=False,
            required_evidence=[tx_id, "DOC-1001"],
            expected_action={"tool": ActionType.ISSUE_REFUND.value, "params": action_params},
            expected_end_state=end_state,
            classification={"category": "billing", "issue": "refund_request", "severity": "medium"},
        )

    @classmethod
    def _solve_subscription_cancellation(
        cls,
        world: dict[str, Any],
        cust_id: str,
        variant: str,
        meta: dict[str, Any],
    ) -> GroundTruth:
        sub_id = meta.get("subscription_id", "")
        sub = next((s for s in world.get("subscriptions", []) if s["id"] == sub_id), None)

        if variant == "missing_info" or not sub:
            return GroundTruth(
                expected_resolution=ResolutionType.REQUEST_INFO,
                must_escalate=False,
                required_evidence=["DOC-1003"],
                expected_action={"tool": ActionType.REQUEST_VERIFICATION.value, "params": {"customer_id": cust_id}},
                expected_end_state=apply_action_to_world(world, "request_verification", {"customer_id": cust_id})[0],
                classification={"category": "subscription", "issue": "cancellation", "severity": "low"},
            )

        # Check lock-in period
        current_dt = parse_iso(world.get("current_date", "2026-09-15T00:00:00Z"))
        lock_in = sub.get("lock_in_until")
        is_locked = False
        if lock_in:
            is_locked = parse_iso(lock_in) > current_dt and not sub.get("has_approved_exception", False)

        if is_locked:
            # Locked in without exception -> deny cancellation
            return GroundTruth(
                expected_resolution=ResolutionType.DENY,
                must_escalate=False,
                required_evidence=[sub_id, "DOC-1003"],
                expected_action={"tool": ActionType.NONE.value, "params": {}},
                expected_end_state=world,
                classification={"category": "subscription", "issue": "cancellation", "severity": "medium"},
            )

        # Eligible cancellation
        action_params = {"customer_id": cust_id, "subscription_id": sub_id}
        end_state, _ = apply_action_to_world(world, "cancel_subscription", action_params)
        return GroundTruth(
            expected_resolution=ResolutionType.REFUND,  # resolved via cancel action
            must_escalate=False,
            required_evidence=[sub_id, "DOC-1003"],
            expected_action={"tool": ActionType.CANCEL_SUBSCRIPTION.value, "params": action_params},
            expected_end_state=end_state,
            classification={"category": "subscription", "issue": "cancellation", "severity": "medium"},
        )

    @classmethod
    def _solve_delivery_dispute(
        cls,
        world: dict[str, Any],
        cust_id: str,
        variant: str,
        meta: dict[str, Any],
    ) -> GroundTruth:
        tx_id = meta.get("transaction_id", "")
        has_signed_pod = meta.get("has_signed_pod", True)
        courier_doc_id = meta.get("courier_doc_id", "DOC-2001")

        if variant == "missing_info":
            return GroundTruth(
                expected_resolution=ResolutionType.REQUEST_INFO,
                must_escalate=False,
                required_evidence=["DOC-1004"],
                expected_action={"tool": ActionType.REQUEST_VERIFICATION.value, "params": {"customer_id": cust_id}},
                expected_end_state=apply_action_to_world(world, "request_verification", {"customer_id": cust_id})[0],
                classification={"category": "fulfillment", "issue": "delivery_dispute", "severity": "low"},
            )

        if has_signed_pod:
            # Signed delivery proof on file -> cannot refund automatically, escalate to logistics (DOC-1004 §2)
            action_params = {
                "case_id": meta.get("case_id", f"CASE-{tx_id}"),
                "team": "logistics_investigations",
                "reason": f"signed courier proof of delivery on file for {tx_id} requires investigation per DOC-1004",
            }
            end_state, _ = apply_action_to_world(world, "escalate_case", action_params)
            return GroundTruth(
                expected_resolution=ResolutionType.ESCALATE,
                must_escalate=True,
                required_evidence=[tx_id, "DOC-1004", courier_doc_id],
                expected_action={"tool": ActionType.ESCALATE_CASE.value, "params": action_params},
                expected_end_state=end_state,
                classification={"category": "fulfillment", "issue": "delivery_dispute", "severity": "high"},
            )
        else:
            # Courier confirms lost in transit -> eligible for refund
            amount = float(meta.get("amount", 199.0))
            action_params = {"transaction_id": tx_id, "amount": amount, "reason": "lost in transit courier confirmed"}
            end_state, _ = apply_action_to_world(world, "issue_refund", action_params)
            return GroundTruth(
                expected_resolution=ResolutionType.REFUND,
                must_escalate=False,
                required_evidence=[tx_id, "DOC-1004", courier_doc_id],
                expected_action={"tool": ActionType.ISSUE_REFUND.value, "params": action_params},
                expected_end_state=end_state,
                classification={"category": "fulfillment", "issue": "delivery_dispute", "severity": "medium"},
            )

    @classmethod
    def _solve_account_lock_fraud(
        cls,
        world: dict[str, Any],
        cust_id: str,
        variant: str,
        meta: dict[str, Any],
    ) -> GroundTruth:
        if variant == "missing_info":
            action_params = {"customer_id": cust_id, "verification_type": "identity"}
            end_state, _ = apply_action_to_world(world, "request_verification", action_params)
            return GroundTruth(
                expected_resolution=ResolutionType.REQUEST_INFO,
                must_escalate=False,
                required_evidence=[cust_id, "DOC-1005"],
                expected_action={"tool": ActionType.REQUEST_VERIFICATION.value, "params": action_params},
                expected_end_state=end_state,
                classification={"category": "security", "issue": "fraud_suspicion", "severity": "low"},
            )

        is_confirmed_breach = meta.get("is_confirmed_breach", False)

        if is_confirmed_breach:
            action_params = {
                "case_id": meta.get("case_id", f"CASE-{cust_id}"),
                "team": "security_operations",
                "reason": f"confirmed foreign unauthorized access on account {cust_id} per DOC-1005",
            }
            end_state, _ = apply_action_to_world(world, "escalate_case", action_params)
            return GroundTruth(
                expected_resolution=ResolutionType.ESCALATE,
                must_escalate=True,
                required_evidence=[cust_id, "DOC-1005"],
                expected_action={"tool": ActionType.ESCALATE_CASE.value, "params": action_params},
                expected_end_state=end_state,
                classification={"category": "security", "issue": "fraud_suspicion", "severity": "high"},
            )
        else:
            # Safe verification fallback (PS §4.2, §5.3, DOC-1005 §2)
            action_params = {"customer_id": cust_id, "verification_type": "identity"}
            end_state, _ = apply_action_to_world(world, "request_verification", action_params)
            return GroundTruth(
                expected_resolution=ResolutionType.REQUEST_INFO,
                must_escalate=False,
                required_evidence=[cust_id, "DOC-1005"],
                expected_action={"tool": ActionType.REQUEST_VERIFICATION.value, "params": action_params},
                expected_end_state=end_state,
                classification={"category": "security", "issue": "fraud_suspicion", "severity": "medium"},
            )

    @classmethod
    def _solve_previous_agent(
        cls,
        world: dict[str, Any],
        cust_id: str,
        variant: str,
        meta: dict[str, Any],
    ) -> GroundTruth:
        if variant == "missing_info":
            action_params = {"customer_id": cust_id, "verification_type": "identity"}
            end_state, _ = apply_action_to_world(world, "request_verification", action_params)
            return GroundTruth(
                expected_resolution=ResolutionType.REQUEST_INFO,
                must_escalate=False,
                required_evidence=["DOC-1842"],
                expected_action={"tool": ActionType.REQUEST_VERIFICATION.value, "params": action_params},
                expected_end_state=end_state,
                classification={"category": "billing", "issue": "previous_agent_wrong", "severity": "low"},
            )

        case_id = meta.get("case_id", "")
        tx_id = meta.get("transaction_id", "")
        # Previous agent erroneously refunded despite active chargeback or stale policy.
        # Current authoritative policy forbids automated refund; requires escalation.
        action_params = {
            "case_id": f"CASE-ESCALATE-{tx_id}",
            "team": "billing_specialists",
            "reason": f"prior ticket {case_id} applied invalid precedent; active chargeback on {tx_id} requires escalation per DOC-1842",
        }
        end_state, _ = apply_action_to_world(world, "escalate_case", action_params)
        return GroundTruth(
            expected_resolution=ResolutionType.ESCALATE,
            must_escalate=True,
            required_evidence=[case_id, tx_id, "DOC-1842"],
            expected_action={"tool": ActionType.ESCALATE_CASE.value, "params": action_params},
            expected_end_state=end_state,
            classification={"category": "billing", "issue": "previous_agent_wrong", "severity": "high"},
        )
