import json
import sys
from pathlib import Path
from typing import Any
from unittest.mock import patch

import httpx
import pytest

ROOT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT_DIR / "starter-kit" / "mock_simulator"))

import server  # noqa: E402
from server import app, get_db_connection, init_db  # noqa: E402

FORBIDDEN_GT_KEYS = {
    "ground_truth",
    "ground_truth_privileged",
    "expected_resolution",
    "must_escalate",
    "required_evidence",
    "expected_action",
    "expected_end_state",
    "diff_explanation",
}


def assert_no_ground_truth_leakage(data: Any, context: str):
    """Recursively audits any serialized response, log, or string for forbidden ground-truth keys."""
    if isinstance(data, dict):
        for k, v in data.items():
            assert k not in FORBIDDEN_GT_KEYS, f"Ground-truth key '{k}' leaked in {context}: {data}"
            assert_no_ground_truth_leakage(v, f"{context}->{k}")
    elif isinstance(data, list):
        for idx, item in enumerate(data):
            assert_no_ground_truth_leakage(item, f"{context}[{idx}]")
    elif isinstance(data, str):
        # Ensure no raw serialized JSON containing ground truth is leaked
        for key in FORBIDDEN_GT_KEYS:
            assert f'"{key}"' not in data, f"Ground-truth key '{key}' found serialized in string ({context}): {data}"


@pytest.fixture(autouse=True)
def setup_mock():
    init_db()


@pytest.mark.asyncio
async def test_ground_truth_revealed_when_flag_enabled():
    """Verifies that when REVEAL_GROUND_TRUTH=true, practice evaluation fields are included."""
    with patch.object(server, "REVEAL_GROUND_TRUTH", True):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            headers = {"Authorization": "Bearer reveal-true-token"}
            await client.post("/dev/reset", headers=headers)
            t_res = await client.post("/task/start", headers=headers)
            task_id = t_res.json()["task_id"]

            submit_res = await client.post(
                "/task/submit",
                json={
                    "task_id": task_id,
                    "case_classification": {"category": "billing", "issue": "dup", "severity": "low"},
                    "decision": {"resolution": "refund", "escalation_required": False},
                    "evidence": ["TXN-1"],
                    "uncertainties": [],
                    "customer_response": "Processed",
                    "confidence": 0.8,
                },
                headers=headers,
            )
            assert submit_res.status_code == 200
            data = submit_res.json()
            assert data["received"] is True
            assert "correct" in data
            assert "expected_resolution" in data
            assert "expected_evidence" in data
            assert "diff_explanation" in data


@pytest.mark.asyncio
async def test_ground_truth_strictly_hidden_when_flag_disabled():
    """Audits all endpoints, logs, and error paths to ensure ZERO ground truth leakage when REVEAL_GROUND_TRUTH=false."""
    with patch.object(server, "REVEAL_GROUND_TRUTH", False):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            headers = {"Authorization": "Bearer reveal-false-token"}
            await client.post("/dev/reset", headers=headers)

            # 1. Audit /task/start
            start_res = await client.post("/task/start", headers=headers)
            assert start_res.status_code == 200
            start_data = start_res.json()
            assert_no_ground_truth_leakage(start_data, "task/start")
            task_id = start_data["task_id"]
            cust_id = start_data["customer_id"]

            # 2. Audit read tools
            sk_res = await client.post("/tools/search_knowledge", json={"query": "policy"}, headers=headers)
            assert_no_ground_truth_leakage(sk_res.json(), "tools/search_knowledge")

            gc_res = await client.post("/tools/get_customer", json={"customer_id": cust_id}, headers=headers)
            assert_no_ground_truth_leakage(gc_res.json(), "tools/get_customer")

            # 3. Audit action tools
            verif_res = await client.post("/tools/request_verification", json={"customer_id": cust_id}, headers=headers)
            assert_no_ground_truth_leakage(verif_res.json(), "tools/request_verification")

            # 4. Audit /task/submit
            submit_res = await client.post(
                "/task/submit",
                json={
                    "task_id": task_id,
                    "case_classification": {"category": "billing", "issue": "dup", "severity": "low"},
                    "decision": {"resolution": "refund", "escalation_required": False},
                    "evidence": ["TXN-1"],
                    "uncertainties": [],
                    "customer_response": "Processed",
                    "confidence": 0.8,
                },
                headers=headers,
            )
            assert submit_res.status_code == 200
            submit_data = submit_res.json()
            assert_no_ground_truth_leakage(submit_data, "task/submit")
            assert submit_data == {"received": True, "task_id": task_id}

            # 5. Audit /submission/*
            sub_start = await client.post("/submission/start", headers=headers)
            assert_no_ground_truth_leakage(sub_start.json(), "submission/start")
            sub_id = sub_start.json()["submission_id"]

            sub_stat = await client.get(f"/submission/{sub_id}/status", headers=headers)
            assert_no_ground_truth_leakage(sub_stat.json(), "submission/status")

            sub_fin = await client.post(f"/submission/{sub_id}/finalize", headers=headers)
            assert_no_ground_truth_leakage(sub_fin.json(), "submission/finalize")

            # 6. Audit SQLite tool call logs
            conn = get_db_connection()
            cursor = conn.cursor()
            cursor.execute("SELECT request_payload, response_payload FROM mock_tool_call_logs")
            rows = cursor.fetchall()
            conn.close()

            for row in rows:
                assert_no_ground_truth_leakage(json.loads(row["request_payload"]), "tool_logs.request")
                assert_no_ground_truth_leakage(json.loads(row["response_payload"]), "tool_logs.response")
