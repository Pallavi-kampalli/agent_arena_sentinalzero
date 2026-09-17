"""SentinelZero participant contract parity tests.

Verifies that the production (src/agent_arena/domain/rules.py) and
mock simulator (starter-kit/mock_simulator/server.py) domain logic
are identical for all SentinelZero operations.
"""

import subprocess
import sys
from pathlib import Path
from typing import Any

import httpx
import pytest

from agent_arena.api.app import app as prod_app
from agent_arena.domain.rules import (
    apply_action_to_world,
    check_escalation_validity,
)

# Import mock simulator server
ROOT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT_DIR / "starter-kit" / "mock_simulator"))

import server  # noqa: E402

mock_app = server.app
mock_apply_action = server.apply_action_to_world
mock_check_escalation = server.check_escalation_validity


@pytest.fixture
def sz_world_state() -> dict[str, Any]:
    """A minimal SentinelZero world state matching the canonical format."""
    return {
        "current_date": "2026-09-15T00:00:00Z",
        "target_message_id": "MSG-SENTINEL-001",
        "directory": [
            {"id": "EMP-001", "name": "Alice Smith", "official_email": "alice@sentinel-acme.edu"},
        ],
        "domains": [
            {"domain_id": "DOM-OFFICIAL-001", "domain": "sentinel-acme.edu", "category": "official"},
        ],
        "threat_intel": [
            {"domain_id": "DOM-THREAT-001", "domain": "phishing-corp.example", "reputation": "malicious"},
        ],
        "security_policies": [
            {"id": "POL-001", "category": "phishing", "title": "Phishing Response Policy"},
        ],
        "policies": [
            {"id": "POL-001", "category": "phishing", "title": "Phishing Response Policy"},
        ],
        "historical_threats": [
            {"log_id": "LOG-001", "threat_type": "phishing", "domain": "phishing-corp.example"},
        ],
        "threads": [],
        "actions_taken": [],
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
# SentinelZero Domain Parity Matrix (12 Scenarios)
# =============================================================================


def test_scenario_01_valid_allow_and_deliver(sz_world_state):
    """allow_and_deliver should mutate delivery_status to 'delivered' in both prod and mock."""
    prod_state, prod_res = apply_action_to_world(
        sz_world_state, "allow_and_deliver", {"message_id": "MSG-SENTINEL-001", "reason": "Internal email from verified sender"}
    )
    mock_state, mock_res = mock_apply_action(
        sz_world_state, "allow_and_deliver", {"message_id": "MSG-SENTINEL-001", "reason": "Internal email from verified sender"}
    )
    assert prod_res.is_eligible is True
    assert mock_res.is_eligible is True
    assert prod_state["delivery_status"] == "delivered"
    assert mock_state["delivery_status"] == "delivered"
    assert prod_res.to_dict() == mock_res.to_dict()


def test_scenario_02_apply_warning_banner(sz_world_state):
    """apply_warning_banner should mutate delivery_status to 'warning_applied' in both."""
    params = {"message_id": "MSG-SENTINEL-001", "banner_type": "EXTERNAL_SENDER", "reason": "Sender domain is external"}
    prod_state, prod_res = apply_action_to_world(sz_world_state, "apply_warning_banner", params)
    mock_state, mock_res = mock_apply_action(sz_world_state, "apply_warning_banner", params)
    assert prod_res.is_eligible is True
    assert mock_res.is_eligible is True
    assert prod_state["delivery_status"] == "warning_applied"
    assert mock_state["delivery_status"] == "warning_applied"
    assert prod_res.to_dict() == mock_res.to_dict()


def test_scenario_03_quarantine_message(sz_world_state):
    """quarantine_message should mutate delivery_status to 'quarantined' in both."""
    params = {"message_id": "MSG-SENTINEL-001", "reason": "Phishing detected: malicious domain in sender"}
    prod_state, prod_res = apply_action_to_world(sz_world_state, "quarantine_message", params)
    mock_state, mock_res = mock_apply_action(sz_world_state, "quarantine_message", params)
    assert prod_res.is_eligible is True
    assert mock_res.is_eligible is True
    assert prod_state["delivery_status"] == "quarantined"
    assert mock_state["delivery_status"] == "quarantined"
    assert prod_res.to_dict() == mock_res.to_dict()


def test_scenario_04_missing_message_id_rejected(sz_world_state):
    """Action with empty/missing message_id must be rejected with INVALID_MESSAGE_ID in both."""
    prod_state, prod_res = apply_action_to_world(
        sz_world_state, "quarantine_message", {"message_id": "", "reason": "no id"}
    )
    mock_state, mock_res = mock_apply_action(
        sz_world_state, "quarantine_message", {"message_id": "", "reason": "no id"}
    )
    assert prod_res.is_eligible is False
    assert mock_res.is_eligible is False
    assert prod_res.error == "INVALID_MESSAGE_ID"
    assert mock_res.error == "INVALID_MESSAGE_ID"
    assert prod_res.to_dict() == mock_res.to_dict()


def test_scenario_05_unknown_action_rejected(sz_world_state):
    """An unknown action type must return UNKNOWN_ACTION in both prod and mock."""
    prod_state, prod_res = apply_action_to_world(
        sz_world_state, "issue_refund", {"transaction_id": "TXN-001", "amount": 50.0}
    )
    mock_state, mock_res = mock_apply_action(
        sz_world_state, "issue_refund", {"transaction_id": "TXN-001", "amount": 50.0}
    )
    assert prod_res.is_eligible is False
    assert mock_res.is_eligible is False
    assert prod_res.error == "UNKNOWN_ACTION"
    assert mock_res.error == "UNKNOWN_ACTION"


def test_scenario_06_actions_taken_audit_trail(sz_world_state):
    """Each action should append to actions_taken in world state in both prod and mock."""
    prod_state, _ = apply_action_to_world(
        sz_world_state, "quarantine_message", {"message_id": "MSG-SENTINEL-001", "reason": "Phishing"}
    )
    mock_state, _ = mock_apply_action(
        sz_world_state, "quarantine_message", {"message_id": "MSG-SENTINEL-001", "reason": "Phishing"}
    )
    assert len(prod_state["actions_taken"]) == 1
    assert len(mock_state["actions_taken"]) == 1
    assert prod_state["actions_taken"][0]["action"] == "quarantine_message"
    assert mock_state["actions_taken"][0]["action"] == "quarantine_message"


def test_scenario_07_grounded_escalation_valid(sz_world_state):
    """Escalation citing a retrieved evidence ID (EMP-001) must succeed in both."""
    retrieved = {"EMP-001"}
    reason = "Sender EMP-001 is not authorised to send from external domain"
    prod_res = check_escalation_validity(sz_world_state, "MSG-SENTINEL-001", reason, retrieved)
    mock_res = mock_check_escalation(sz_world_state, "MSG-SENTINEL-001", reason, retrieved)
    assert prod_res.is_eligible is True
    assert mock_res.is_eligible is True
    assert prod_res.to_dict() == mock_res.to_dict()


def test_scenario_08_ungrounded_escalation_rejected(sz_world_state):
    """Escalation with no evidence ID in reason must be rejected in both."""
    retrieved = {"EMP-001"}
    reason = "Customer is very angry and yelling at me"
    prod_res = check_escalation_validity(sz_world_state, "MSG-SENTINEL-001", reason, retrieved)
    mock_res = mock_check_escalation(sz_world_state, "MSG-SENTINEL-001", reason, retrieved)
    assert prod_res.is_eligible is False
    assert mock_res.is_eligible is False
    assert prod_res.reason == "reason_not_grounded"
    assert mock_res.reason == "reason_not_grounded"
    assert prod_res.to_dict() == mock_res.to_dict()


def test_scenario_09_short_reason_rejected(sz_world_state):
    """Escalation with very short reason must be rejected in both."""
    prod_res = check_escalation_validity(sz_world_state, "MSG-SENTINEL-001", "ok", {})
    mock_res = mock_check_escalation(sz_world_state, "MSG-SENTINEL-001", "ok", {})
    assert prod_res.is_eligible is False
    assert mock_res.is_eligible is False
    assert prod_res.reason == "reason_not_grounded"
    assert mock_res.reason == "reason_not_grounded"


def test_scenario_10_escalation_with_dom_evidence(sz_world_state):
    """Escalation citing a domain ID (DOM-THREAT-001) must succeed in both."""
    retrieved = {"DOM-THREAT-001"}
    reason = "Domain DOM-THREAT-001 is on the threat intelligence blacklist"
    prod_res = check_escalation_validity(sz_world_state, "MSG-SENTINEL-001", reason, retrieved)
    mock_res = mock_check_escalation(sz_world_state, "MSG-SENTINEL-001", reason, retrieved)
    assert prod_res.is_eligible is True
    assert mock_res.is_eligible is True
    assert prod_res.to_dict() == mock_res.to_dict()


def test_scenario_11_escalate_to_tier2_as_action(sz_world_state):
    """escalate_to_tier2_soc action with grounded reason should update world state in both."""
    retrieved = {"DOM-THREAT-001"}
    params = {
        "message_id": "MSG-SENTINEL-001",
        "reason": "Active threat intelligence match DOM-THREAT-001 requires human SOC review",
    }
    prod_state, prod_res = apply_action_to_world(sz_world_state, "escalate_to_tier2_soc", params, retrieved)
    mock_state, mock_res = mock_apply_action(sz_world_state, "escalate_to_tier2_soc", params, retrieved)
    assert prod_res.is_eligible is True
    assert mock_res.is_eligible is True
    assert prod_state.get("delivery_status") == "escalated_to_soc"
    assert mock_state.get("delivery_status") == "escalated_to_soc"
    assert prod_res.to_dict() == mock_res.to_dict()


def test_scenario_12_ungrounded_escalate_no_mutation(sz_world_state):
    """Ungrounded escalate_to_tier2_soc must NOT mutate world state in either prod or mock."""
    import copy
    initial_state = copy.deepcopy(sz_world_state)
    params = {"message_id": "MSG-SENTINEL-001", "reason": "This message looks fishy to me"}
    prod_state, prod_res = apply_action_to_world(sz_world_state, "escalate_to_tier2_soc", params, set())
    mock_state, mock_res = mock_apply_action(sz_world_state, "escalate_to_tier2_soc", params, set())
    assert prod_res.is_eligible is False
    assert mock_res.is_eligible is False
    assert prod_state is sz_world_state  # Returns original, not copy
    assert prod_res.to_dict() == mock_res.to_dict()
