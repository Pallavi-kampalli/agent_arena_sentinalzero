import subprocess
import sys
from pathlib import Path
from typing import Any

import httpx
import pytest

from agent_arena.api.app import app as prod_app
from agent_arena.domain.rules import (
    apply_action_to_world,
    check_cancellation_eligibility,
    check_escalation_validity,
    check_refund_eligibility,
)

# Import mock simulator server
ROOT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT_DIR / "starter-kit" / "mock_simulator"))

import server  # noqa: E402

mock_app = server.app
mock_apply_action = server.apply_action_to_world
mock_check_cancellation = server.check_cancellation_eligibility
mock_check_escalation = server.check_escalation_validity
mock_check_refund = server.check_refund_eligibility


@pytest.fixture
def sample_world_state() -> dict[str, Any]:
    return {
        "current_date": "2026-09-15T00:00:00Z",
        "customers": [
            {"id": "CUS-100", "name": "Alice Smith", "tier": "gold"},
            {"id": "CUS-200", "name": "Bob Jones", "tier": "standard"},
        ],
        "transactions": [
            {
                "id": "TXN-001",
                "customer_id": "CUS-100",
                "amount": 100.0,
                "refunded_amount": 0.0,
                "refund_status": "settled",
                "date": "2026-09-10T00:00:00Z",
                "chargeback_status": "none",
                "under_fraud_investigation": False,
            },
            {
                "id": "TXN-HOLD",
                "customer_id": "CUS-100",
                "amount": 200.0,
                "refunded_amount": 0.0,
                "refund_status": "settled",
                "date": "2026-09-10T00:00:00Z",
                "chargeback_status": "investigation_active",
                "under_fraud_investigation": False,
            },
            {
                "id": "TXN-OLD",
                "customer_id": "CUS-100",
                "amount": 50.0,
                "refunded_amount": 0.0,
                "refund_status": "settled",
                "date": "2026-07-01T00:00:00Z",
                "chargeback_status": "none",
                "under_fraud_investigation": False,
            },
        ],
        "subscriptions": [
            {
                "id": "SUB-001",
                "customer_id": "CUS-100",
                "status": "active",
                "lock_in_until": "2026-08-01T00:00:00Z",  # In the past
                "has_approved_exception": False,
                "has_unresolved_dispute": False,
            },
            {
                "id": "SUB-LOCKED",
                "customer_id": "CUS-100",
                "status": "active",
                "lock_in_until": "2026-12-01T00:00:00Z",  # In the future
                "has_approved_exception": False,
                "has_unresolved_dispute": False,
            },
            {
                "id": "SUB-EXCEPT",
                "customer_id": "CUS-100",
                "status": "active",
                "lock_in_until": "2026-12-01T00:00:00Z",
                "has_approved_exception": True,
                "has_unresolved_dispute": False,
            },
            {
                "id": "SUB-DISPUTE",
                "customer_id": "CUS-100",
                "status": "active",
                "lock_in_until": "2026-08-01T00:00:00Z",
                "has_approved_exception": False,
                "has_unresolved_dispute": True,
            },
        ],
        "policies": [
            {
                "id": "DOC-1001",
                "category": "refund",
                "title": "Authoritative Refund Policy",
                "updated_at": "2026-09-01T00:00:00Z",
                "rules": {"refund_window_days": 30},
            },
            {
                "id": "DOC-1003",
                "category": "cancellation",
                "title": "Authoritative Cancellation Policy",
                "updated_at": "2026-09-01T00:00:00Z",
            },
            {
                "id": "DOC-1842",
                "category": "dispute_hold",
                "title": "Dispute Hold Policy",
                "updated_at": "2026-09-01T00:00:00Z",
            },
        ],
        "documents": [],
        "historical_cases": [],
    }


def test_stale_artifact_ci_guard():
    """Verifies that starter-kit/mock_simulator artifacts match canonical generator without staleness."""
    export_script = ROOT_DIR / "scripts" / "export_starter_kit.py"
    res = subprocess.run([sys.executable, str(export_script), "--check"], capture_output=True, text=True)
    assert res.returncode == 0, f"Stale-artifact check failed:\n{res.stdout}\n{res.stderr}"


@pytest.mark.asyncio
async def test_dev_reset_is_mock_only():
    """Verifies that /dev/reset returns 200 on mock simulator and does not exist in production API."""
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=mock_app), base_url="http://test") as client:
        mock_resp = await client.post("/dev/reset", headers={"Authorization": "Bearer dev-test-token"})
        assert mock_resp.status_code == 200
        assert mock_resp.json().get("reset") is True

    # Production API must NOT have /dev/reset route
    prod_paths = [getattr(r, "path", None) for r in prod_app.routes]
    assert "/dev/reset" not in prod_paths


# =============================================================================
# 12-Scenario Comprehensive Domain Parity Matrix
# =============================================================================


def test_scenario_01_valid_refund(sample_world_state):
    prod_res = check_refund_eligibility(sample_world_state, "TXN-001", 50.0, "customer returned item")
    mock_res = mock_check_refund(sample_world_state, "TXN-001", 50.0, "customer returned item")
    assert prod_res.is_eligible is True
    assert mock_res.is_eligible is True
    assert prod_res.to_dict() == mock_res.to_dict()

    prod_state, _ = apply_action_to_world(
        sample_world_state, "issue_refund", {"transaction_id": "TXN-001", "amount": 50.0}
    )
    mock_state, _ = mock_apply_action(sample_world_state, "issue_refund", {"transaction_id": "TXN-001", "amount": 50.0})
    assert prod_state == mock_state


def test_scenario_02_refund_amount_exceeds(sample_world_state):
    prod_res = check_refund_eligibility(sample_world_state, "TXN-001", 150.0, "customer asking too much")
    mock_res = mock_check_refund(sample_world_state, "TXN-001", 150.0, "customer asking too much")
    assert prod_res.is_eligible is False
    assert mock_res.is_eligible is False
    assert prod_res.reason == "amount_exceeds_transaction"
    assert mock_res.reason == "amount_exceeds_transaction"
    assert prod_res.to_dict() == mock_res.to_dict()


def test_scenario_03_already_refunded(sample_world_state):
    mutated, _ = apply_action_to_world(
        sample_world_state, "issue_refund", {"transaction_id": "TXN-001", "amount": 100.0}
    )
    prod_res = check_refund_eligibility(mutated, "TXN-001", 10.0, "refund again")
    mock_res = mock_check_refund(mutated, "TXN-001", 10.0, "refund again")
    assert prod_res.is_eligible is False
    assert mock_res.is_eligible is False
    assert prod_res.reason == "already_refunded"
    assert mock_res.reason == "already_refunded"
    assert prod_res.to_dict() == mock_res.to_dict()


def test_scenario_04_chargeback_investigation_hold(sample_world_state):
    prod_res = check_refund_eligibility(sample_world_state, "TXN-HOLD", 200.0, "refund under dispute")
    mock_res = mock_check_refund(sample_world_state, "TXN-HOLD", 200.0, "refund under dispute")
    assert prod_res.is_eligible is False
    assert mock_res.is_eligible is False
    assert prod_res.reason == "chargeback_investigation_active"
    assert mock_res.reason == "chargeback_investigation_active"
    assert prod_res.policy_ref == "DOC-1842"
    assert mock_res.policy_ref == "DOC-1842"
    assert prod_res.to_dict() == mock_res.to_dict()


def test_scenario_05_outside_refund_policy_window(sample_world_state):
    prod_res = check_refund_eligibility(sample_world_state, "TXN-OLD", 50.0, "old transaction refund")
    mock_res = mock_check_refund(sample_world_state, "TXN-OLD", 50.0, "old transaction refund")
    assert prod_res.is_eligible is False
    assert mock_res.is_eligible is False
    assert prod_res.reason == "outside_refund_window"
    assert mock_res.reason == "outside_refund_window"
    assert prod_res.to_dict() == mock_res.to_dict()


def test_scenario_06_valid_cancellation(sample_world_state):
    prod_res = check_cancellation_eligibility(sample_world_state, "CUS-100", "SUB-001")
    mock_res = mock_check_cancellation(sample_world_state, "CUS-100", "SUB-001")
    assert prod_res.is_eligible is True
    assert mock_res.is_eligible is True
    assert prod_res.to_dict() == mock_res.to_dict()

    prod_state, _ = apply_action_to_world(
        sample_world_state, "cancel_subscription", {"customer_id": "CUS-100", "subscription_id": "SUB-001"}
    )
    mock_state, _ = mock_apply_action(
        sample_world_state, "cancel_subscription", {"customer_id": "CUS-100", "subscription_id": "SUB-001"}
    )
    assert prod_state == mock_state


def test_scenario_07_contractual_lock_in_active(sample_world_state):
    prod_res = check_cancellation_eligibility(sample_world_state, "CUS-100", "SUB-LOCKED")
    mock_res = mock_check_cancellation(sample_world_state, "CUS-100", "SUB-LOCKED")
    assert prod_res.is_eligible is False
    assert mock_res.is_eligible is False
    assert prod_res.reason == "lock_in_period_active"
    assert mock_res.reason == "lock_in_period_active"
    assert prod_res.to_dict() == mock_res.to_dict()


def test_scenario_08_cancellation_with_approved_exception(sample_world_state):
    prod_res = check_cancellation_eligibility(sample_world_state, "CUS-100", "SUB-EXCEPT")
    mock_res = mock_check_cancellation(sample_world_state, "CUS-100", "SUB-EXCEPT")
    assert prod_res.is_eligible is True
    assert mock_res.is_eligible is True
    assert prod_res.to_dict() == mock_res.to_dict()


def test_scenario_09_unresolved_billing_dispute(sample_world_state):
    prod_res = check_cancellation_eligibility(sample_world_state, "CUS-100", "SUB-DISPUTE")
    mock_res = mock_check_cancellation(sample_world_state, "CUS-100", "SUB-DISPUTE")
    assert prod_res.is_eligible is False
    assert mock_res.is_eligible is False
    assert prod_res.reason == "unresolved_billing_dispute"
    assert mock_res.reason == "unresolved_billing_dispute"
    assert prod_res.to_dict() == mock_res.to_dict()


def test_scenario_10_valid_grounded_escalation(sample_world_state):
    retrieved = {"TXN-HOLD", "DOC-1842"}
    reason = "Active investigation on TXN-HOLD requires escalation per DOC-1842"
    prod_res = check_escalation_validity(sample_world_state, "CASE-01", "fraud_team", reason, retrieved)
    mock_res = mock_check_escalation(sample_world_state, "CASE-01", "fraud_team", reason, retrieved)
    assert prod_res.is_eligible is True
    assert mock_res.is_eligible is True
    assert prod_res.to_dict() == mock_res.to_dict()


def test_scenario_11_invalid_ungrounded_escalation(sample_world_state):
    retrieved = {"TXN-001"}
    reason = "Customer is very angry and yelling at me"
    prod_res = check_escalation_validity(sample_world_state, "CASE-01", "support", reason, retrieved)
    mock_res = mock_check_escalation(sample_world_state, "CASE-01", "support", reason, retrieved)
    assert prod_res.is_eligible is False
    assert mock_res.is_eligible is False
    assert prod_res.reason == "reason_not_grounded"
    assert mock_res.reason == "reason_not_grounded"
    assert prod_res.to_dict() == mock_res.to_dict()


def test_scenario_12_verification_safe_fallback(sample_world_state):
    prod_state, prod_res = apply_action_to_world(
        sample_world_state, "request_verification", {"customer_id": "CUS-100", "verification_type": "identity"}
    )
    mock_state, mock_res = mock_apply_action(
        sample_world_state, "request_verification", {"customer_id": "CUS-100", "verification_type": "identity"}
    )
    assert prod_res.is_eligible is True
    assert mock_res.is_eligible is True
    assert prod_res.to_dict() == mock_res.to_dict()
    assert prod_state == mock_state
