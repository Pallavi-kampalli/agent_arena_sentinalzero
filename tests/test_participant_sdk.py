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
    """Verifies SDK dispatch for all 6 read tools."""
    mock_httpx = MagicMock()
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {"results": [{"id": "DOC-1"}]}
    mock_httpx.post.return_value = mock_resp

    client = ToolsClient(base_url="http://localhost:8000")
    client._client = mock_httpx

    # 1. search_knowledge
    res = client.search_knowledge("refund policy", top_k=3)
    assert res == {"results": [{"id": "DOC-1"}]}
    mock_httpx.post.assert_called_with("/tools/search_knowledge", json={"query": "refund policy", "top_k": 3})

    # 2. get_document
    mock_resp.json.return_value = {"document": {"id": "DOC-1"}}
    res = client.get_document("DOC-1")
    assert res["document"]["id"] == "DOC-1"
    mock_httpx.post.assert_called_with("/tools/get_document", json={"document_id": "DOC-1"})

    # 3. get_customer
    mock_resp.json.return_value = {"customer": {"id": "CUS-1"}}
    res = client.get_customer("CUS-1")
    assert res["customer"]["id"] == "CUS-1"
    mock_httpx.post.assert_called_with("/tools/get_customer", json={"customer_id": "CUS-1"})

    # 4. get_transactions
    mock_resp.json.return_value = {"transactions": [{"id": "TXN-1"}]}
    res = client.get_transactions("CUS-1", start_date="2026-01-01")
    assert "transactions" in res
    mock_httpx.post.assert_called_with(
        "/tools/get_transactions", json={"customer_id": "CUS-1", "start_date": "2026-01-01"}
    )

    # 5. get_subscription
    mock_resp.json.return_value = {"subscription": {"id": "SUB-1"}}
    res = client.get_subscription("CUS-1")
    assert res["subscription"]["id"] == "SUB-1"
    mock_httpx.post.assert_called_with("/tools/get_subscription", json={"customer_id": "CUS-1"})

    # 6. get_previous_cases
    mock_resp.json.return_value = {"cases": [{"case_id": "CASE-1"}]}
    res = client.get_previous_cases("CUS-1", limit=2)
    assert "cases" in res
    mock_httpx.post.assert_called_with("/tools/get_previous_cases", json={"customer_id": "CUS-1", "limit": 2})


def test_sdk_preserves_domain_rejections():
    """Verifies that HTTP 200 INELIGIBLE responses are preserved as dicts, not raised as exceptions."""
    mock_httpx = MagicMock()
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "error": "INELIGIBLE",
        "reason": "chargeback_investigation_active",
        "policy_ref": "DOC-1842",
    }
    mock_httpx.post.return_value = mock_resp

    client = ToolsClient()
    client._client = mock_httpx

    result = client.issue_refund("TXN-1", 100.0, "reason")
    assert result.get("error") == "INELIGIBLE"
    assert result.get("reason") == "chargeback_investigation_active"
    assert result.get("policy_ref") == "DOC-1842"


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
