import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from agent_arena.models.task import Task
from agent_arena.services.auth_service import register_team
from agent_arena.services.settings_service import SettingsService
from agent_arena.services.tool_service import ToolService
from conftest import generate_world


@pytest.fixture
async def setup_team_and_task(db_session: AsyncSession):
    """Sets up default settings, a registered team, and an assigned task."""
    settings_service = SettingsService(db_session)
    await settings_service.seed_defaults()

    # 1. Register team
    team, token = await register_team(
        session=db_session,
        team_name="CyberHawks",
        members=[{"name": "Alice", "email": "alice@example.com"}],
    )

    # 2. Create baseline world & task in tasks table
    world = generate_world(seed=42)
    task = Task(
        task_id="TASK-READ-001",
        dataset="dev",
        family="refund_request",
        variant="normal",
        input_payload={"customer_id": "CUS-1001", "customer_message": "Please refund me."},
        world_state_seed=world,
        ground_truth={"expected_resolution": "refund", "must_escalate": False, "required_evidence": ["DOC-1001"]},
    )
    db_session.add(task)
    await db_session.commit()

    # 3. Assign task to team
    tool_service = ToolService(db_session, settings_service)
    assignment = await tool_service.assign_task(team.team_id, "TASK-READ-001")

    return {
        "team": team,
        "token": token,
        "task": task,
        "assignment": assignment,
        "world": world,
    }


@pytest.mark.asyncio
async def test_search_knowledge_success(client: AsyncClient, setup_team_and_task):
    """Verify search_knowledge returns relevant policies with snippets and updated_at."""
    token = setup_team_and_task["token"]
    headers = {"Authorization": f"Bearer {token}"}

    resp = await client.post("/tools/search_knowledge", json={"query": "refund policy", "top_k": 3}, headers=headers)
    assert resp.status_code == 200
    data = resp.json()

    assert "results" in data
    results = data["results"]
    assert len(results) <= 3
    assert len(results) > 0

    # Top result should be refund policy
    top = results[0]
    assert "id" in top
    assert "title" in top
    assert "snippet" in top
    assert "updated_at" in top
    assert "DOC-" in top["id"]


@pytest.mark.asyncio
async def test_get_document_success_and_not_found(client: AsyncClient, setup_team_and_task):
    """Verify get_document retrieves existing document and returns 404 for unknown document."""
    token = setup_team_and_task["token"]
    headers = {"Authorization": f"Bearer {token}"}

    # Found
    resp = await client.post("/tools/get_document", json={"document_id": "DOC-1001"}, headers=headers)
    assert resp.status_code == 200
    data = resp.json()
    assert "document" in data
    doc = data["document"]
    assert doc["id"] == "DOC-1001"
    assert "Authoritative Customer Refund Policy" in doc["title"]
    assert "rules" in doc

    # Not found
    resp_404 = await client.post("/tools/get_document", json={"document_id": "DOC-NONEXISTENT"}, headers=headers)
    assert resp_404.status_code == 404
    err = resp_404.json()
    assert "detail" in err
    assert err["detail"]["error"] == "DOCUMENT_NOT_FOUND"


@pytest.mark.asyncio
async def test_get_customer_success_and_not_found(client: AsyncClient, setup_team_and_task):
    """Verify get_customer returns customer details or 404."""
    token = setup_team_and_task["token"]
    headers = {"Authorization": f"Bearer {token}"}

    # Found
    resp = await client.post("/tools/get_customer", json={"customer_id": "CUS-1001"}, headers=headers)
    assert resp.status_code == 200
    cust = resp.json()["customer"]
    assert cust["id"] == "CUS-1001"
    assert "tier" in cust
    assert "verification_status" in cust

    # Not found
    resp_404 = await client.post("/tools/get_customer", json={"customer_id": "CUS-GHOST-999"}, headers=headers)
    assert resp_404.status_code == 404
    assert resp_404.json()["detail"]["error"] == "CUSTOMER_NOT_FOUND"


@pytest.mark.asyncio
async def test_get_transactions_date_filtering(client: AsyncClient, setup_team_and_task):
    """Verify get_transactions returns transactions and filters by date bounds."""
    token = setup_team_and_task["token"]
    headers = {"Authorization": f"Bearer {token}"}

    # Unfiltered
    resp = await client.post("/tools/get_transactions", json={"customer_id": "CUS-1001"}, headers=headers)
    assert resp.status_code == 200
    txs = resp.json()["transactions"]
    assert isinstance(txs, list)

    # Filter with tight date window
    resp_filtered = await client.post(
        "/tools/get_transactions",
        json={
            "customer_id": "CUS-1001",
            "start_date": "2026-08-01T00:00:00Z",
            "end_date": "2026-09-01T00:00:00Z",
        },
        headers=headers,
    )
    assert resp_filtered.status_code == 200

    # Malformed date format
    resp_invalid = await client.post(
        "/tools/get_transactions",
        json={"customer_id": "CUS-1001", "start_date": "not-a-valid-date"},
        headers=headers,
    )
    assert resp_invalid.status_code == 422


@pytest.mark.asyncio
async def test_get_subscription_success(client: AsyncClient, setup_team_and_task):
    """Verify get_subscription returns subscription or null."""
    token = setup_team_and_task["token"]
    headers = {"Authorization": f"Bearer {token}"}

    resp = await client.post("/tools/get_subscription", json={"customer_id": "CUS-1001"}, headers=headers)
    assert resp.status_code == 200
    data = resp.json()
    assert "subscription" in data
    if data["subscription"] is not None:
        assert "plan" in data["subscription"]
        assert "billing_cycle" in data["subscription"]


@pytest.mark.asyncio
async def test_get_previous_cases(client: AsyncClient, setup_team_and_task):
    """Verify get_previous_cases returns historical tickets capped by limit."""
    token = setup_team_and_task["token"]
    headers = {"Authorization": f"Bearer {token}"}

    resp = await client.post("/tools/get_previous_cases", json={"customer_id": "CUS-1001", "limit": 2}, headers=headers)
    assert resp.status_code == 200
    cases = resp.json()["cases"]
    assert isinstance(cases, list)
    assert len(cases) <= 2


@pytest.mark.asyncio
async def test_unauthenticated_request_rejected(client: AsyncClient):
    """Verify requests without Bearer token return 401."""
    resp = await client.post("/tools/get_customer", json={"customer_id": "CUS-1001"})
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_no_active_task_returns_404(client: AsyncClient, db_session: AsyncSession):
    """Verify team with no active task assignment receives 404 NO_ACTIVE_TASK."""
    settings_service = SettingsService(db_session)
    await settings_service.seed_defaults()

    team, token = await register_team(
        session=db_session,
        team_name="TasklessTeam",
        members=[{"name": "Bob"}],
    )
    headers = {"Authorization": f"Bearer {token}"}

    resp = await client.post("/tools/get_customer", json={"customer_id": "CUS-1001"}, headers=headers)
    assert resp.status_code == 404
    assert resp.json()["detail"]["error"] == "NO_ACTIVE_TASK"
