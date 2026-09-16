import pytest
import sqlalchemy as sa
from conftest import generate_world
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from agent_arena.config import get_config
from agent_arena.models.setting import SettingsAuditLog
from agent_arena.models.task import Task
from agent_arena.services.settings_service import SettingsService


@pytest.fixture
def admin_headers():
    secret = get_config().ADMIN_PANEL_SECRET
    return {"X-Admin-Secret": secret}


@pytest.fixture
async def seeded_tasks(db_session: AsyncSession):
    settings = SettingsService(db_session)
    await settings.seed_defaults()
    for i in range(3):
        t_id = f"TASK-ADM-{i:03d}"
        world = generate_world(seed=4000 + i)
        task = Task(
            task_id=t_id,
            dataset="hidden",
            input_payload={"customer_id": f"CUS-{i}", "customer_message": "Need refund"},
            world_state_seed=world,
            ground_truth={"expected_resolution": "refund", "must_escalate": False},
        )
        db_session.add(task)
    await settings.set("hidden_task_count", 3)
    await db_session.commit()


@pytest.mark.asyncio
async def test_admin_create_team_success_and_env_snippet(client: AsyncClient, admin_headers):
    """Admin can create a new team, getting back token and .env snippet."""
    payload = {
        "team_name": "CyberDynasty",
        "members": [{"name": "Ada Lovelace", "email": "ada@example.com"}],
        "github_repo_url": "https://github.com/cyber/dynasty",
    }
    resp = await client.post("/admin/teams", json=payload, headers=admin_headers)
    assert resp.status_code == 201
    data = resp.json()

    assert data["team_name"] == "CyberDynasty"
    assert "token" in data
    assert len(data["token"]) > 20
    assert "env_snippet" in data
    assert f"AGENT_ARENA_TEAM_ID={data['team_id']}" in data["env_snippet"]
    assert f"AGENT_ARENA_BEARER_TOKEN={data['token']}" in data["env_snippet"]
    assert "AGENT_ARENA_BASE_URL=" in data["env_snippet"]


@pytest.mark.asyncio
async def test_admin_create_duplicate_team_rejected(client: AsyncClient, admin_headers):
    """Creating a team with an existing name returns 409 Conflict."""
    payload = {"team_name": "UniqueTeam1"}
    resp1 = await client.post("/admin/teams", json=payload, headers=admin_headers)
    assert resp1.status_code == 201

    resp2 = await client.post("/admin/teams", json=payload, headers=admin_headers)
    assert resp2.status_code == 409
    assert resp2.json()["detail"]["error"] == "TEAM_NAME_EXISTS"


@pytest.mark.asyncio
async def test_admin_list_and_filter_teams(client: AsyncClient, admin_headers):
    """Admin can list teams with pagination and search filter."""
    for i in range(5):
        await client.post("/admin/teams", json={"team_name": f"AlphaTeam_{i}"}, headers=admin_headers)

    # Search filter
    resp = await client.get("/admin/teams?search=AlphaTeam_2", headers=admin_headers)
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 1
    assert data["teams"][0]["team_name"] == "AlphaTeam_2"

    # Ensure no token hashes or salts leaked
    assert "token" not in data["teams"][0]
    assert "token_hash" not in data["teams"][0]
    assert "bearer_token_hash" not in data["teams"][0]


@pytest.mark.asyncio
async def test_admin_update_team_metadata(client: AsyncClient, admin_headers):
    """Admin can update team name and members."""
    resp = await client.post("/admin/teams", json={"team_name": "BeforeUpdate"}, headers=admin_headers)
    team_id = resp.json()["team_id"]

    patch_resp = await client.patch(
        f"/admin/teams/{team_id}",
        json={"team_name": "AfterUpdate", "github_repo_url": "https://github.com/updated/repo"},
        headers=admin_headers,
    )
    assert patch_resp.status_code == 200
    assert patch_resp.json()["team_name"] == "AfterUpdate"
    assert patch_resp.json()["github_repo_url"] == "https://github.com/updated/repo"


@pytest.mark.asyncio
async def test_admin_team_status_transitions_and_enforcement(
    client: AsyncClient, admin_headers, db_session: AsyncSession, seeded_tasks
):
    """Admin can suspend, activate, and disqualify teams, and participant requests enforce status."""
    # 1. Create team
    create_resp = await client.post("/admin/teams", json={"team_name": "StatusTestTeam"}, headers=admin_headers)
    team_id = create_resp.json()["team_id"]
    token = create_resp.json()["token"]
    part_headers = {"Authorization": f"Bearer {token}"}

    # 2. Verify participant endpoint works while active
    start_resp = await client.post("/submission/start", headers=part_headers)
    assert start_resp.status_code == 200
    sub_id = start_resp.json()["submission_id"]

    # 3. Suspend team
    susp_resp = await client.put(
        f"/admin/teams/{team_id}/status",
        json={"status": "suspended"},
        headers=admin_headers,
    )
    assert susp_resp.status_code == 200
    assert susp_resp.json()["status"] == "suspended"

    # 4. Participant endpoints fail with 403 when suspended
    fail_start = await client.post("/submission/start", headers=part_headers)
    assert fail_start.status_code == 403
    assert fail_start.json()["error"] == "FORBIDDEN"
    assert "suspended" in fail_start.json()["detail"]

    task_start = await client.post(
        "/task/start",
        json={"submission_id": sub_id, "task_id": "TASK-ADM-000"},
        headers=part_headers,
    )
    assert task_start.status_code == 403
    assert task_start.json()["error"] == "FORBIDDEN"

    # 5. Check audit log has entry for suspension
    audit = (
        await db_session.execute(sa.select(SettingsAuditLog).where(SettingsAuditLog.key == f"team:{team_id}:status"))
    ).scalar_one_or_none()
    assert audit is not None
    assert audit.new_value == {"status": "suspended"}

    # 6. Reactivate team
    act_resp = await client.put(
        f"/admin/teams/{team_id}/status",
        json={"status": "active"},
        headers=admin_headers,
    )
    assert act_resp.status_code == 200
    assert act_resp.json()["status"] == "active"

    # Participant endpoints work again
    task_start2 = await client.post(
        "/task/start",
        json={"submission_id": sub_id, "task_id": "TASK-ADM-000"},
        headers=part_headers,
    )
    assert task_start2.status_code == 200

    # 7. Invalid status rejected
    bad_resp = await client.put(
        f"/admin/teams/{team_id}/status",
        json={"status": "deleted"},
        headers=admin_headers,
    )
    assert bad_resp.status_code in (400, 422)


@pytest.mark.asyncio
async def test_admin_regenerate_token_immediate_revocation(
    client: AsyncClient, admin_headers, db_session: AsyncSession, seeded_tasks
):
    """Regenerating a team token immediately revokes the old token."""
    # 1. Create team
    create_resp = await client.post("/admin/teams", json={"team_name": "TokenRevokeTeam"}, headers=admin_headers)
    team_id = create_resp.json()["team_id"]
    old_token = create_resp.json()["token"]

    # 2. Confirm old token works
    resp1 = await client.post("/submission/start", headers={"Authorization": f"Bearer {old_token}"})
    assert resp1.status_code == 200

    # 3. Admin regenerates token
    regen_resp = await client.post(f"/admin/teams/{team_id}/token", headers=admin_headers)
    assert regen_resp.status_code == 200
    regen_data = regen_resp.json()
    new_token = regen_data["token"]
    assert new_token != old_token
    assert regen_data["token_version"] == 2
    assert f"AGENT_ARENA_BEARER_TOKEN={new_token}" in regen_data["env_snippet"]

    # 4. Old token fails immediately on next request
    old_resp = await client.post("/submission/start", headers={"Authorization": f"Bearer {old_token}"})
    assert old_resp.status_code == 401
    assert old_resp.json()["error"] == "UNAUTHORIZED"
    assert "TOKEN_REVOKED" in old_resp.json()["detail"]

    # 5. New token succeeds immediately on participant endpoints
    sub_id = resp1.json()["submission_id"]
    status_resp = await client.get(f"/submission/{sub_id}/status", headers={"Authorization": f"Bearer {new_token}"})
    assert status_resp.status_code == 200
    assert status_resp.json()["status"] == "in_progress"


@pytest.mark.asyncio
async def test_admin_bulk_import_csv(client: AsyncClient, admin_headers):
    """Admin can bulk import teams via CSV and receive env snippets."""
    csv_data = (
        "team_name,member_names,member_emails,github_repo_url\n"
        "BulkTeamOne,Alice Smith,alice@bulk.com,https://github.com/bulk/one\n"
        "BulkTeamTwo,Bob Jones;Carol Danvers,bob@bulk.com;carol@bulk.com,https://github.com/bulk/two\n"
    )

    resp = await client.post(
        "/admin/teams/bulk-import",
        json={"csv_content": csv_data},
        headers=admin_headers,
    )
    assert resp.status_code == 201
    data = resp.json()
    assert data["total_rows"] == 2
    assert data["created_count"] == 2
    assert data["skipped_count"] == 0
    assert len(data["teams"]) == 2
    assert "BulkTeamOne" in [t["team_name"] for t in data["teams"]]
    assert "BulkTeamTwo" in [t["team_name"] for t in data["teams"]]

    # Re-importing same CSV should skip both existing teams gracefully
    resp2 = await client.post(
        "/admin/teams/bulk-import",
        json={"csv_content": csv_data},
        headers=admin_headers,
    )
    assert resp2.status_code == 201
    data2 = resp2.json()
    assert data2["created_count"] == 0
    assert data2["skipped_count"] == 2


@pytest.mark.asyncio
async def test_admin_bulk_import_missing_header(client: AsyncClient, admin_headers):
    """Bulk import rejects CSV without team_name header."""
    csv_data = "name,email\nInvalid,no_header@example.com"
    resp = await client.post(
        "/admin/teams/bulk-import",
        json={"csv_content": csv_data},
        headers=admin_headers,
    )
    assert resp.status_code == 400
    assert resp.json()["detail"]["error"] == "INVALID_CSV_HEADER"
