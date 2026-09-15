import pytest

from agent_arena.domain.rules import (
    apply_action_to_world,
    check_cancellation_eligibility,
    check_escalation_validity,
    check_refund_eligibility,
)
from agent_arena.world.generator import generate_world


@pytest.fixture
def test_world():
    return generate_world(seed=42)


def test_refund_chargeback_investigation_hold(test_world):
    """Verify refund is rejected with DOC-1842 when chargeback investigation is active (PS §5.2)."""
    # Create a transaction with active chargeback
    tx_id = "TXN-TEST-CB"
    test_world["transactions"].append(
        {
            "id": tx_id,
            "customer_id": "CUS-1001",
            "amount": 499.0,
            "date": "2026-09-10T00:00:00Z",
            "status": "completed",
            "chargeback_status": "investigation_active",
            "under_fraud_investigation": True,
            "refund_status": "none",
        }
    )

    result = check_refund_eligibility(test_world, tx_id, amount=499.0, reason="customer request")
    assert result.is_eligible is False
    assert result.error == "INELIGIBLE"
    assert result.reason == "chargeback_investigation_active"
    assert result.policy_ref == "DOC-1842"


def test_refund_outside_window(test_world):
    """Verify refund is rejected when transaction is older than 30-day window (DOC-1001)."""
    tx_id = "TXN-TEST-OLD"
    test_world["transactions"].append(
        {
            "id": tx_id,
            "customer_id": "CUS-1001",
            "amount": 100.0,
            "date": "2026-07-01T00:00:00Z",  # > 70 days ago
            "status": "completed",
            "chargeback_status": "none",
            "under_fraud_investigation": False,
            "refund_status": "none",
        }
    )

    result = check_refund_eligibility(test_world, tx_id, amount=100.0, reason="late request")
    assert result.is_eligible is False
    assert result.error == "INELIGIBLE"
    assert result.reason == "outside_refund_window"
    assert result.policy_ref == "DOC-1001"


def test_cancellation_lock_in_period(test_world):
    """Verify cancellation is rejected during active lock-in without approved exception."""
    sub_id = "SUB-TEST-LOCK"
    test_world["subscriptions"].append(
        {
            "id": sub_id,
            "customer_id": "CUS-1001",
            "plan": "pro_annual",
            "billing_cycle": "annual",
            "status": "active",
            "start_date": "2026-06-01T00:00:00Z",
            "lock_in_until": "2027-06-01T00:00:00Z",
            "has_approved_exception": False,
        }
    )

    result = check_cancellation_eligibility(test_world, "CUS-1001", sub_id)
    assert result.is_eligible is False
    assert result.error == "INELIGIBLE"
    assert result.reason == "lock_in_period_active"
    assert result.policy_ref == "DOC-1003"


def test_cancellation_with_approved_exception(test_world):
    """Verify cancellation succeeds when approved exception flag is present."""
    sub_id = "SUB-TEST-EXCEPT"
    test_world["subscriptions"].append(
        {
            "id": sub_id,
            "customer_id": "CUS-1001",
            "plan": "pro_annual",
            "billing_cycle": "annual",
            "status": "active",
            "start_date": "2026-06-01T00:00:00Z",
            "lock_in_until": "2027-06-01T00:00:00Z",
            "has_approved_exception": True,
        }
    )

    result = check_cancellation_eligibility(test_world, "CUS-1001", sub_id)
    assert result.is_eligible is True
    assert result.status == "cancelled"


def test_escalation_requires_grounded_evidence(test_world):
    """Verify escalation is rejected if reason does not cite retrievable evidence (PS §5.3)."""
    # Ungrounded reason
    bad_res = check_escalation_validity(test_world, "CASE-1", team="billing", reason="customer is really angry")
    assert bad_res.is_eligible is False
    assert bad_res.error == "INVALID_ESCALATION"
    assert bad_res.reason == "reason_not_grounded"

    # Grounded reason citing DOC-1842
    good_res = check_escalation_validity(
        test_world, "CASE-1", team="billing_specialists", reason="active chargeback investigation per DOC-1842"
    )
    assert good_res.is_eligible is True
    assert good_res.status == "escalated"


def test_ineligible_action_leaves_state_unchanged(test_world):
    """Verify world state is completely unmodified when an action fails enforcement (PS §5.1)."""
    tx_id = "TXN-TEST-CB2"
    test_world["transactions"].append(
        {
            "id": tx_id,
            "customer_id": "CUS-1001",
            "amount": 300.0,
            "date": "2026-09-12T00:00:00Z",
            "status": "completed",
            "chargeback_status": "investigation_active",
            "refund_status": "none",
            "refunded_amount": 0.0,
        }
    )

    new_state, res = apply_action_to_world(
        test_world, "issue_refund", {"transaction_id": tx_id, "amount": 300.0, "reason": "test"}
    )
    assert res.is_eligible is False
    # Transaction in new_state must still have refund_status == "none"
    tx_in_new = next(t for t in new_state["transactions"] if t["id"] == tx_id)
    assert tx_in_new["refund_status"] == "none"
    assert tx_in_new["refunded_amount"] == 0.0
