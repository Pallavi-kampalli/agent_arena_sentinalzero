import hashlib
import sys
from pathlib import Path

import httpx
import pytest

ROOT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT_DIR / "starter-kit" / "mock_simulator"))

from server import app, get_db_connection, init_db  # noqa: E402


@pytest.fixture(autouse=True)
def setup_mock_db():
    init_db()


@pytest.mark.asyncio
async def test_mock_tasks_table_loaded():
    """Verifies that public dev tasks are loaded into SQLite mock_tasks table."""
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT COUNT(*) FROM mock_tasks")
    count = cursor.fetchone()[0]
    conn.close()
    assert count == 30


@pytest.mark.asyncio
async def test_task_start_and_isolation():
    """Verifies task assignment and session isolation using SHA-256 hashed session IDs."""
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        # Reset session
        await client.post("/dev/reset", headers={"Authorization": "Bearer token-alpha"})
        await client.post("/dev/reset", headers={"Authorization": "Bearer token-beta"})

        # Session Alpha starts task
        res_a = await client.post("/task/start", headers={"Authorization": "Bearer token-alpha"})
        assert res_a.status_code == 200
        task_a = res_a.json()
        assert "task_id" in task_a

        # Session Beta starts task
        res_b = await client.post("/task/start", headers={"Authorization": "Bearer token-beta"})
        assert res_b.status_code == 200
        task_b = res_b.json()
        assert "task_id" in task_b

        # Verify session IDs in SQLite are SHA-256 hashes, not raw tokens
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT session_id, task_id FROM mock_task_assignments")
        assignments = cursor.fetchall()
        conn.close()

        expected_hash_a = hashlib.sha256(b"token-alpha").hexdigest()[:16]
        expected_hash_b = hashlib.sha256(b"token-beta").hexdigest()[:16]

        session_ids = [row["session_id"] for row in assignments]
        assert expected_hash_a in session_ids
        assert expected_hash_b in session_ids
        assert "token-alpha" not in session_ids
        assert "token-beta" not in session_ids


@pytest.mark.asyncio
async def test_mock_read_tools_execution():
    """Tests SentinelZero read tools against the active task runtime state."""
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        headers = {"Authorization": "Bearer test-read-token"}
        await client.post("/dev/reset", headers=headers)
        start_res = await client.post("/task/start", headers=headers)
        task = start_res.json()

        # 1. lookup_directory
        dir_res = await client.post(
            "/tools/lookup_directory", json={"identifier": "alex.smith@sentinel-acme.edu"}, headers=headers
        )
        assert dir_res.status_code == 200

        # 2. get_approved_domains
        dom_res = await client.post("/tools/get_approved_domains", json={}, headers=headers)
        assert dom_res.status_code == 200

        # 3. get_email_headers
        hdr_res = await client.post("/tools/get_email_headers", json={"message_id": "MSG-DEV-001"}, headers=headers)
        assert hdr_res.status_code == 200

        # 4. inspect_domain_reputation
        rep_res = await client.post("/tools/inspect_domain_reputation", json={"domain": "apex-labs-procurement.xyz"}, headers=headers)
        assert rep_res.status_code == 200

        # 5. get_thread_history
        th_res = await client.post("/tools/get_thread_history", json={"thread_id": "THR-DEV-001"}, headers=headers)
        assert th_res.status_code == 200


@pytest.mark.asyncio
async def test_mock_action_tools_and_logging():
    """Tests action tools execution and tool logging."""
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        headers = {"Authorization": "Bearer test-action-token"}
        await client.post("/dev/reset", headers=headers)
        start_res = await client.post("/task/start", headers=headers)

        # Action tool call
        act_res = await client.post("/tools/allow_and_deliver", json={"message_id": "MSG-DEV-001", "reason": "Test delivery"}, headers=headers)
        assert act_res.status_code == 200

        # Check logs in SQLite
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT tool_name, was_enforcement_rejection, latency_ms FROM mock_tool_call_logs")
        logs = cursor.fetchall()
        conn.close()

        tool_names = [log["tool_name"] for log in logs]
        assert "allow_and_deliver" in tool_names


@pytest.mark.asyncio
async def test_mock_submission_lifecycle():
    """Tests the /submission/* lifecycle endpoints in the mock simulator."""
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        headers = {"Authorization": "Bearer test-submission-token"}
        await client.post("/dev/reset", headers=headers)

        # 1. Start submission
        sub_start = await client.post("/submission/start", headers=headers)
        assert sub_start.status_code == 200
        data = sub_start.json()
        sub_id = data["submission_id"]
        assert data["attempt_number"] == 1
        assert data["tasks_total"] == 30

        # 2. Get status
        status_res = await client.get(f"/submission/{sub_id}/status", headers=headers)
        assert status_res.status_code == 200
        status_data = status_res.json()
        assert status_data["status"] == "in_progress"
        assert status_data["tasks_total"] == 30

        # 3. Finalize
        fin_res = await client.post(f"/submission/{sub_id}/finalize", headers=headers)
        assert fin_res.status_code == 200
        assert fin_res.json()["status"] == "completed"

        # 4. Status after finalize
        status_after = await client.get(f"/submission/{sub_id}/status", headers=headers)
        assert status_after.json()["status"] == "completed"
