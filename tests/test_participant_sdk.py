import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

ROOT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT_DIR / "starter-kit"))

from sdk.tools_client import ApiError, ToolsClient, TransportError  # noqa: E402


def test_sdk_init_defaults():
    """Verifies SDK constructor respects environment variables and default fallbacks."""
    with patch.dict(os.environ, {"BASE_URL": "http://arena.test:9000", "BEARER_TOKEN": "secret-token"}):
        client = ToolsClient()
        assert client.base_url == "http://arena.test:9000"
        assert client.token == "secret-token"

    # Explicit constructor arguments override environment variables
    client_custom = ToolsClient(base_url="http://custom:8080/", token="custom-token")
    assert client_custom.base_url == "http://custom:8080"  # Strips trailing slash
    assert client_custom.token == "custom-token"


def test_sdk_read_tools():
    """Verifies SDK dispatch for all 5 SentinelZero read tools."""
    mock_httpx = MagicMock()
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {"found": True, "employee": {"id": "EMP-1001"}}
    mock_httpx.post.return_value = mock_resp

    client = ToolsClient(base_url="http://localhost:8000")
    client._client = mock_httpx

    # 1. lookup_directory
    res = client.lookup_directory("alice@sentinel-acme.edu")
    assert res == {"found": True, "employee": {"id": "EMP-1001"}}
    mock_httpx.post.assert_called_with("/tools/lookup_directory", json={"identifier": "alice@sentinel-acme.edu"})

    # 2. get_approved_domains
    mock_resp.json.return_value = {"official_domains": ["sentinel-acme.edu"]}
    res = client.get_approved_domains()
    assert res["official_domains"] == ["sentinel-acme.edu"]
    mock_httpx.post.assert_called_with("/tools/get_approved_domains", json={})

    # 3. get_email_headers
    mock_resp.json.return_value = {"message_id": "MSG-001"}
    res = client.get_email_headers("MSG-001")
    assert res["message_id"] == "MSG-001"
    mock_httpx.post.assert_called_with("/tools/get_email_headers", json={"message_id": "MSG-001"})

    # 4. inspect_domain_reputation
    mock_resp.json.return_value = {"reputation": "trusted"}
    res = client.inspect_domain_reputation("sentinel-acme.edu")
    assert res["reputation"] == "trusted"
    mock_httpx.post.assert_called_with("/tools/inspect_domain_reputation", json={"domain": "sentinel-acme.edu"})

    # 5. get_thread_history
    mock_resp.json.return_value = {"messages": []}
    res = client.get_thread_history("THR-001")
    assert "messages" in res
    mock_httpx.post.assert_called_with("/tools/get_thread_history", json={"thread_id": "THR-001"})


def test_sdk_preserves_domain_rejections():
    """Verifies that HTTP 200 INELIGIBLE/INVALID_ESCALATION responses are preserved as dicts, not raised as exceptions."""
    mock_httpx = MagicMock()
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "error": "INVALID_ESCALATION",
        "reason": "reason_not_grounded",
    }
    mock_httpx.post.return_value = mock_resp

    client = ToolsClient()
    client._client = mock_httpx

    result = client.escalate_to_tier2_soc("MSG-001", "un-grounded reason")
    assert result.get("error") == "INVALID_ESCALATION"
    assert result.get("reason") == "reason_not_grounded"


def test_sdk_raises_api_error_on_http_failure():
    """Verifies that HTTP 4xx/5xx responses raise ApiError."""
    mock_httpx = MagicMock()
    mock_resp = MagicMock()
    mock_resp.status_code = 404
    mock_resp.json.return_value = {"error": "NOT_FOUND", "message": "Resource missing"}
    mock_httpx.post.return_value = mock_resp

    client = ToolsClient()
    client._client = mock_httpx

    with pytest.raises(ApiError) as exc_info:
        client.get_customer("CUS-NONEXISTENT")
    assert exc_info.value.status_code == 404
    assert exc_info.value.detail["error"] == "NOT_FOUND"


def test_sdk_raises_transport_error_on_network_failure():
    """Verifies that connection failures raise TransportError."""
    # Connect to an invalid local port that refuses connections
    client = ToolsClient(base_url="http://127.0.0.1:59999", timeout=0.5)
    with pytest.raises(TransportError):
        client.start_task()


def test_sdk_task_and_submission_flow():
    """Verifies SDK task and submission flow dispatch."""
    mock_httpx = MagicMock()
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {"task_id": "TASK-01", "customer_id": "CUS-1", "customer_message": "Hello"}
    mock_httpx.post.return_value = mock_resp
    mock_httpx.get.return_value = mock_resp

    client = ToolsClient()
    client._client = mock_httpx

    # start_task
    task = client.start_task()
    assert task["task_id"] == "TASK-01"

    # submit_task with explicit parameters
    mock_resp.json.return_value = {"received": True, "task_id": "TASK-01"}
    sub_res = client.submit_task(
        task_id="TASK-01",
        case_classification={"category": "billing", "issue": "dup", "severity": "low"},
        decision={"resolution": "refund", "escalation_required": False},
        evidence=["TXN-1"],
        uncertainties=[],
        customer_response="Refunded",
        confidence=0.9,
    )
    assert sub_res["received"] is True

    # submit_task with agent output payload dict
    sub_res_payload = client.submit_task(
        task_id="TASK-01",
        payload={
            "case_classification": {"category": "billing", "issue": "dup", "severity": "low"},
            "decision": {"resolution": "refund", "escalation_required": False},
            "evidence": ["TXN-1"],
            "uncertainties": [],
            "customer_response": "Refunded",
            "confidence": 0.9,
        },
    )
    assert sub_res_payload["received"] is True

    # start_submission
    mock_resp.json.return_value = {"submission_id": "sub-123", "attempt_number": 1, "tasks_total": 70}
    s_start = client.start_submission()
    assert s_start["submission_id"] == "sub-123"

    # get_submission_status
    mock_resp.json.return_value = {
        "status": "in_progress",
        "tasks_completed": 5,
        "tasks_total": 70,
        "time_remaining_seconds": 1200,
    }
    s_stat = client.get_submission_status("sub-123")
    assert s_stat["status"] == "in_progress"

    # finalize_submission
    mock_resp.json.return_value = {"submission_id": "sub-123", "status": "completed"}
    s_fin = client.finalize_submission("sub-123")
    assert s_fin["status"] == "completed"
