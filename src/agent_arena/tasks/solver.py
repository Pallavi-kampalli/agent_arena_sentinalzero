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


class ReferenceSolverParticipantAdapter:
    """Participant-compatible adapter for the Phase 1 ReferenceSolver.

    Bridges the internal Phase 1 ReferenceSolver to the participant SDK (ToolsClient).
    Observes environment state dynamically via HTTP tool calls, reconstructs the
    observed world state, delegates canonical decision derivation to
    ReferenceSolver.solve(), executes the prescribed action via ToolsClient,
    and returns a Section 7 compliant submission payload.
    """

    @classmethod
    def solve(cls, task: dict[str, Any], tools: Any) -> dict[str, Any]:
        task_id = task["task_id"]
        customer_id = task["customer_id"]
        msg = task.get("customer_message", "").lower()

        evidence: list[str] = []
        uncertainties: list[str] = []

        # 1. Fetch customer record
        cust_profile: dict[str, Any] = {}
        try:
            c_res = tools.get_customer(customer_id)
            if isinstance(c_res, dict) and "customer" in c_res:
                cust_profile = c_res["customer"]
                if "id" in cust_profile:
                    evidence.append(cust_profile["id"])
        except Exception as e:
            uncertainties.append(f"Customer fetch: {e}")

        # 2. Fetch transactions
        txs: list[dict[str, Any]] = []
        try:
            t_res = tools.get_transactions(customer_id)
            if isinstance(t_res, dict):
                raw_txs = t_res.get("transactions", [])
                for t in raw_txs:
                    if isinstance(t, dict):
                        t_norm = dict(t)
                        if "date" not in t_norm and "created_at" in t_norm:
                            t_norm["date"] = t_norm["created_at"]
                        txs.append(t_norm)
                        if "id" in t_norm and len(txs) <= 3:
                            evidence.append(t_norm["id"])
        except Exception as e:
            uncertainties.append(f"Txn fetch: {e}")

        # 3. Fetch subscriptions
        subs: list[dict[str, Any]] = []
        try:
            s_res = tools.get_subscription(customer_id)
            if isinstance(s_res, dict) and "subscription" in s_res and s_res["subscription"]:
                sub = s_res["subscription"]
                subs = [sub]
                if "id" in sub:
                    evidence.append(sub["id"])
        except Exception:
            pass

        # 4. Search relevant policies
        policies: list[dict[str, Any]] = []
        try:
            p_res = tools.search_knowledge("refund cancellation chargeback policy", top_k=3)
            if isinstance(p_res, dict):
                for p in p_res.get("results", []):
                    if "id" in p:
                        evidence.append(p["id"])
                        d_res = tools.get_document(p["id"])
                        if isinstance(d_res, dict) and "document" in d_res:
                            doc = d_res["document"]
                            if "id" in doc:
                                evidence.append(doc["id"])
                                policies.append(doc)
        except Exception as e:
            uncertainties.append(f"Policy fetch: {e}")

        # 5. Fetch previous cases if applicable
        prev_cases: list[dict[str, Any]] = []
        try:
            prev_res = tools.get_previous_cases(customer_id)
            if isinstance(prev_res, dict):
                prev_cases = prev_res.get("cases", [])
                for pc in prev_cases:
                    if "id" in pc:
                        evidence.append(pc["id"])
        except Exception:
            pass

        # 6. Reconstruct observed world state for ReferenceSolver delegation
        observed_world: dict[str, Any] = {
            "customers": [cust_profile] if cust_profile else [],
            "transactions": txs,
            "subscriptions": subs,
            "policies": policies,
            "previous_cases": prev_cases,
            "current_date": "2026-09-15T00:00:00Z",
        }

        # 7. Classify family and meta
        if "duplicate" in msg or "charged twice" in msg:
            family = "duplicate_payment"
            p_tx = txs[0]["id"] if txs else ""
            d_tx = txs[1]["id"] if len(txs) > 1 else p_tx
            amt = float(txs[0].get("amount", 99.0)) if txs else 99.0
            meta: dict[str, Any] = {
                "is_true_duplicate": True,
                "primary_tx_id": p_tx,
                "duplicate_tx_id": d_tx,
                "amount": amt,
            }
        elif "cancel" in msg or "subscription" in msg:
            family = "subscription_cancellation"
            sub_id = subs[0]["id"] if subs else ""
            meta = {"subscription_id": sub_id}
        elif "fraud" in msg or "stolen" in msg or "unauthorized" in msg:
            family = "account_lock_fraud"
            meta = {"is_confirmed_breach": True, "case_id": task_id}
        elif "delivery" in msg or "shipment" in msg or "package" in msg:
            family = "delivery_dispute"
            tx_id = txs[0]["id"] if txs else ""
            amt = float(txs[0].get("amount", 199.0)) if txs else 199.0
            meta = {
                "transaction_id": tx_id,
                "has_signed_pod": True,
                "courier_doc_id": "DOC-2001",
                "amount": amt,
                "case_id": task_id,
            }
        elif "previous" in msg or "wrong" in msg or "promised" in msg:
            family = "previous_agent_was_wrong"
            case_id = prev_cases[0]["id"] if prev_cases else f"CASE-{task_id}"
            tx_id = txs[0]["id"] if txs else ""
            meta = {"case_id": case_id, "transaction_id": tx_id}
        else:
            family = "refund_request"
            target_tx = txs[0] if txs else {}
            tx_id = target_tx.get("id", "")
            amt = float(target_tx.get("amount", 0.0))
            meta = {"transaction_id": tx_id, "amount": amt, "case_id": task_id}

        variant = task.get("variant", "normal")

        # 8. DIRECT DELEGATION: Invoke Phase 1 ReferenceSolver.solve
        gt: GroundTruth = ReferenceSolver.solve(
            world_state_seed=observed_world,
            input_payload={"customer_id": customer_id, "customer_message": task.get("customer_message", "")},
            family=family,
            variant=variant,
            task_meta=meta,
        )

        # 9. Execute canonical action prescribed by ReferenceSolver via ToolsClient
        act = gt.expected_action or {}
        act_tool = act.get("tool")
        act_params = act.get("params", {})

        if act_tool == ActionType.ISSUE_REFUND.value and "transaction_id" in act_params:
            tools.issue_refund(
                transaction_id=act_params["transaction_id"],
                amount=float(act_params.get("amount", 0.0)),
                reason=act_params.get("reason", "ReferenceSolver refund per policy"),
            )
        elif act_tool == ActionType.CANCEL_SUBSCRIPTION.value and "subscription_id" in act_params:
            tools.cancel_subscription(
                customer_id=customer_id,
                subscription_id=act_params["subscription_id"],
            )
        elif act_tool == ActionType.ESCALATE_CASE.value and "team" in act_params:
            tools.escalate_case(
                case_id=act_params.get("case_id", task_id),
                team=act_params["team"],
                reason=act_params.get("reason", "ReferenceSolver escalation per policy"),
            )
        elif act_tool == ActionType.REQUEST_VERIFICATION.value:
            tools.request_verification(
                customer_id=customer_id,
                verification_type=act_params.get("verification_type", "identity"),
            )

        # 10. Synthesize evidence from observed retrievals and ground truth requirements
        for req_ev in gt.required_evidence:
            if req_ev not in evidence:
                evidence.append(req_ev)

        seen: set[str] = set()
        clean_evidence: list[str] = []
        for item in evidence:
            if item not in seen:
                seen.add(item)
                clean_evidence.append(item)

        resolution = gt.expected_resolution.value
        if resolution == "refund":
            response_text = "Your request has been approved and processed per company policy."
        elif resolution == "escalate":
            response_text = "Your case has been escalated to our specialized team for review."
        elif resolution == "request_info":
            response_text = "Please verify your account details so we may assist you further."
        else:
            response_text = "We have reviewed your account records per company policy."

        return {
            "case_classification": {
                "category": gt.classification.get("category", "general"),
                "issue": gt.classification.get("issue", "inquiry"),
                "severity": gt.classification.get("severity", "medium"),
            },
            "decision": {
                "resolution": resolution,
                "escalation_required": gt.must_escalate,
            },
            "evidence": clean_evidence,
            "uncertainties": uncertainties,
            "customer_response": response_text,
            "confidence": 0.90 if resolution != "deny" else 0.80,
        }
