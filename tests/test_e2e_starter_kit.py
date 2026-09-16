import socket
import sys
import threading
import time
from pathlib import Path

import pytest
import uvicorn

ROOT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT_DIR / "starter-kit"))
sys.path.insert(0, str(ROOT_DIR / "starter-kit" / "mock_simulator"))

from agent import solve  # noqa: E402
from sdk.tools_client import ApiError, ToolsClient  # noqa: E402
from server import app, init_db  # noqa: E402


def get_free_port() -> int:
    s = socket.socket()
    s.bind(("", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@pytest.fixture(scope="module")
def mock_server():
    """Spins up the mock simulator on a live localhost port in a background thread."""
    init_db()
    port = get_free_port()
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    # Wait for server to become responsive
    base_url = f"http://127.0.0.1:{port}"
    max_wait = 5.0
    t0 = time.time()
    while time.time() - t0 < max_wait:
        try:
            import httpx

            r = httpx.get(f"{base_url}/health", timeout=0.5)
            if r.status_code == 200:
                break
        except Exception:
            time.sleep(0.05)

    yield base_url
    server.should_exit = True


def test_starter_agent_end_to_end_against_mock(mock_server):
    """Verifies starter kit agent template raises NotImplementedError and contract submission succeeds."""
    tools = ToolsClient(base_url=mock_server, token="dev-starter-token")

    # Reset state for clean run
    tools._post("/dev/reset")

    # 1. Start task
    task = tools.start_task()
    assert "task_id" in task
    assert "customer_id" in task
    assert "customer_message" in task
    task_id = task["task_id"]

    # 2. Run agent - verifies zero solution logic template invariant
    with pytest.raises(NotImplementedError):
        solve(task, tools)

    # 3. Verify Section 7 contract adherence with valid submission payload
    valid_payload = {
        "case_classification": {"category": "billing", "issue": "duplicate_payment", "severity": "medium"},
        "decision": {"resolution": "refund", "escalation_required": False},
        "evidence": ["DOC-REFUND-001"],
        "uncertainties": [],
        "customer_response": "We have processed your duplicate charge refund.",
        "confidence": 0.95,
    }

    # 4. Submit task
    result = tools.submit_task(
        task_id=task_id,
        payload=valid_payload,
    )

    assert result["received"] is True
    assert result["task_id"] == task_id
    assert "correct" in result  # Ground truth revealed in mock practice mode
    assert "diff_explanation" in result
    tools.close()


def test_naive_agent_fails_naturally_without_environment_crash(mock_server):
    """Verifies that a naive agent attempting an illegal action receives a domain rejection without server failure."""
    tools = ToolsClient(base_url=mock_server, token="dev-naive-token")

    tools._post("/dev/reset")
    task = tools.start_task()
    task_id = task["task_id"]

    # Naive agent immediately attempts an invalid refund with bad transaction ID
    with pytest.raises(ApiError) as exc_info:
        tools.issue_refund(
            transaction_id="TXN-INVALID-9999",
            amount=99999.0,
            reason="I want all the money",
        )
    assert exc_info.value.status_code == 404

    # Server remains healthy and responsive
    health = tools._get("/health")
    assert health["status"] == "ok"

    # Now attempt submission with hallucinated evidence
    naive_submit = tools.submit_task(
        task_id=task_id,
        case_classification={"category": "billing", "issue": "general", "severity": "low"},
        decision={"resolution": "refund", "escalation_required": False},
        evidence=["HALLUCINATED-DOC-9999"],
        uncertainties=["Did not read anything"],
        customer_response="Here is your money",
        confidence=1.0,
    )

    assert naive_submit["received"] is True
    # In mock practice mode, diff_explanation explains why the naive agent failed
    assert naive_submit.get("correct") is False
    assert "Missing required evidence" in naive_submit.get("diff_explanation", "") or "mismatch" in naive_submit.get(
        "diff_explanation", ""
    )
    tools.close()
