import asyncio
import uuid
from datetime import UTC, datetime

import pytest
from conftest import generate_world
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from agent_arena.config import get_config
from agent_arena.models.submission import Submission
from agent_arena.models.task import Task
from agent_arena.models.task_assignment import TaskAssignment
from agent_arena.models.team import Team
from agent_arena.scoring.service import ScoringService
from agent_arena.services.auth_service import hash_token, register_team
from agent_arena.services.settings_service import SettingsService
from agent_arena.services.tool_service import ToolService


@pytest.fixture
def admin_headers():
    secret = get_config().ADMIN_PANEL_SECRET
    return {"X-Admin-Secret": secret}


@pytest.mark.asyncio
async def test_concurrent_quarantine_race(client: AsyncClient, db_session: AsyncSession):
    """Verify concurrent quarantine requests on the same message serialize cleanly."""
    settings_service = SettingsService(db_session)
    await settings_service.seed_defaults()

    team, token = await register_team(db_session, f"QuarantineRaceTeam_{uuid.uuid4().hex[:6]}")
    world = generate_world(seed=101)
    msg_id = "MSG-RACE-QUAR"

    task = Task(
        task_id=f"TASK-QUAR-RACE-{uuid.uuid4().hex[:6]}",
        dataset="hidden",
        input_payload={"message_id": msg_id, "sender": "attacker@evil.example"},
        world_state_seed=world,
        ground_truth={"expected_resolution": "quarantine"},
    )
    db_session.add(task)
    await db_session.commit()

    tool_service = ToolService(db_session, settings_service)
    assignment = await tool_service.assign_task(team.team_id, task.task_id)

    headers = {"Authorization": f"Bearer {token}", "X-Task-ID": task.task_id}
    req_body = {"message_id": msg_id, "reason": "Concurrent quarantine race"}

    # Fire two concurrent quarantine requests
    resps = await asyncio.gather(
        client.post("/tools/quarantine_message", json=req_body, headers=headers),
        client.post("/tools/quarantine_message", json=req_body, headers=headers),
    )

    assert all(r.status_code == 200 for r in resps)
    results = [r.json() for r in resps]
    successes = [r for r in results if r.get("status") == "quarantined"]

    assert len(successes) >= 1, f"Expected at least 1 success, got: {results}"

    # Database verification
    assign_id = assignment.id
    db_session.expire_all()
    assign_db = await db_session.get(TaskAssignment, assign_id)
    assert assign_db is not None
    assert assign_db.world_runtime_state.get("delivery_status") == "quarantined"


@pytest.mark.asyncio
async def test_concurrent_allow_race(client: AsyncClient, db_session: AsyncSession):
    """Verify concurrent allow requests on the same message serialize cleanly."""
    settings_service = SettingsService(db_session)
    await settings_service.seed_defaults()

    team, token = await register_team(db_session, f"AllowRaceTeam_{uuid.uuid4().hex[:6]}")
    world = generate_world(seed=102)
    msg_id = "MSG-RACE-ALLOW"

    task = Task(
        task_id=f"TASK-ALLOW-RACE-{uuid.uuid4().hex[:6]}",
        dataset="hidden",
        input_payload={"message_id": msg_id, "sender": "alice@sentinel-acme.edu"},
        world_state_seed=world,
        ground_truth={"expected_resolution": "allow"},
    )
    db_session.add(task)
    await db_session.commit()

    tool_service = ToolService(db_session, settings_service)
    assignment = await tool_service.assign_task(team.team_id, task.task_id)

    headers = {"Authorization": f"Bearer {token}", "X-Task-ID": task.task_id}
    req_body = {"message_id": msg_id, "reason": "Concurrent allow race"}

    # Fire two concurrent allow requests
    resps = await asyncio.gather(
        client.post("/tools/allow_and_deliver", json=req_body, headers=headers),
        client.post("/tools/allow_and_deliver", json=req_body, headers=headers),
    )

    assert all(r.status_code == 200 for r in resps)
    results = [r.json() for r in resps]
    successes = [r for r in results if r.get("status") in ("delivered", "allowed", "allowed_and_delivered")]

    assert len(successes) >= 1, f"Expected at least 1 success, got: {results}"

    # Database verification
    assign_id = assignment.id
    db_session.expire_all()
    assign_db = await db_session.get(TaskAssignment, assign_id)
    assert assign_db is not None
    assert assign_db.world_runtime_state.get("delivery_status") in ("delivered", "allowed")


@pytest.mark.asyncio
async def test_token_revocation_under_concurrent_load(client: AsyncClient, admin_headers, db_session: AsyncSession):
    """Verifies that admin token regeneration immediately revokes the old token under traffic."""
    settings_service = SettingsService(db_session)
    await settings_service.seed_defaults()

    create_resp = await client.post(
        "/admin/teams",
        json={"team_name": f"RevokeLoadTeam_{uuid.uuid4().hex[:6]}"},
        headers=admin_headers,
    )
    assert create_resp.status_code == 201
    team_data = create_resp.json()
    team_id = team_data["team_id"]
    old_token = team_data["token"]

    old_headers = {"Authorization": f"Bearer {old_token}"}

    # Seed a task and set hidden_task_count = 1 so submission/start succeeds
    task = Task(
        task_id=f"TASK-REVOKE-{uuid.uuid4().hex[:6]}",
        dataset="hidden",
        input_payload={"customer_id": "C-01", "customer_message": "Hi"},
        world_state_seed=generate_world(seed=999),
        ground_truth={"expected_resolution": "refund", "must_escalate": False},
    )
    db_session.add(task)
    await settings_service.set("hidden_task_count", 1)
    await db_session.commit()

    # Initial request with old token succeeds
    resp1 = await client.post("/submission/start", headers=old_headers)
    assert resp1.status_code == 200
    sub_id = resp1.json()["submission_id"]

    # Admin regenerates token
    regen_resp = await client.post(f"/admin/teams/{team_id}/token", headers=admin_headers)
    assert regen_resp.status_code == 200
    new_token = regen_resp.json()["token"]
    assert new_token != old_token
    new_headers = {"Authorization": f"Bearer {new_token}"}

    # Under concurrent calls, old token MUST fail with 401, new token MUST succeed
    results = await asyncio.gather(
        client.get(f"/submission/{sub_id}/status", headers=old_headers),
        client.get(f"/submission/{sub_id}/status", headers=old_headers),
        client.get(f"/submission/{sub_id}/status", headers=new_headers),
        client.get(f"/submission/{sub_id}/status", headers=new_headers),
    )

    old_resps = results[:2]
    new_resps = results[2:]

    for r in old_resps:
        assert r.status_code == 401
        assert r.json().get("error") == "UNAUTHORIZED"

    for r in new_resps:
        assert r.status_code == 200
        assert r.json()["status"] == "in_progress"


@pytest.mark.asyncio
async def test_admin_scoreboard_strict_consistency(client: AsyncClient, admin_headers, db_session: AsyncSession):
    """Proves exact consistency between Submission record, ScoringService, and Admin Leaderboard."""
    settings_service = SettingsService(db_session)
    await settings_service.seed_defaults()

    team_id = uuid.uuid4()
    team_name = f"ConsistencyTeam_{uuid.uuid4().hex[:6]}"
    team = Team(
        team_id=team_id,
        team_name=team_name,
        bearer_token_hash=hash_token("tok-consist"),
        token_version=1,
        status="active",
    )
    db_session.add(team)

    sub_id = uuid.uuid4()
    sub = Submission(
        submission_id=sub_id,
        team_id=team_id,
        attempt_number=1,
        status="completed",
        started_at=datetime.now(UTC),
        completed_at=datetime.now(UTC),
    )
    db_session.add(sub)

    # Seed 1 hidden task
    task = Task(
        task_id=f"TASK-SCORE-CONSIST-{uuid.uuid4().hex[:6]}",
        dataset="hidden",
        input_payload={"customer_id": "C-01", "customer_message": "Hi"},
        world_state_seed=generate_world(seed=555),
        ground_truth={"expected_resolution": "refund", "must_escalate": False, "required_evidence": ["DOC-1"]},
    )
    db_session.add(task)
    await db_session.commit()

    # 1. Score through ScoringService
    scoring_svc = ScoringService(db_session, settings_service)
    score_result = await scoring_svc.score_submission(sub_id)
    service_score = score_result.aggregate_score

    # 2. Query Submission directly from DB
    db_session.expire_all()
    sub_db = await db_session.get(Submission, sub_id)
    assert sub_db is not None
    db_score = float(sub_db.aggregate_score)

    # 3. Query Admin Leaderboard endpoint
    lb_resp = await client.get("/admin/leaderboard", headers=admin_headers)
    assert lb_resp.status_code == 200
    lb_data = lb_resp.json()

    # Find the team entry in leaderboard
    team_entries = [e for e in lb_data["leaderboard"] if e["team_id"] == str(team_id)]
    assert len(team_entries) == 1
    leaderboard_score = float(team_entries[0]["aggregate_score"])

    # Strict consistency assertion: db_score == service_score == leaderboard_score
    assert db_score == service_score, f"Drift between DB ({db_score}) and Service ({service_score})"
    assert service_score == leaderboard_score, (
        f"Drift between Service ({service_score}) and Leaderboard ({leaderboard_score})"
    )


@pytest.mark.asyncio
async def test_concurrent_submission_finalize_race(client: AsyncClient, db_session: AsyncSession):
    """Verify concurrent finalize calls for the same submission succeed idempotently without race conditions."""
    settings_service = SettingsService(db_session)
    await settings_service.seed_defaults()

    team, token = await register_team(db_session, f"FinalizeRaceTeam_{uuid.uuid4().hex[:6]}")
    sub_id = uuid.uuid4()
    sub = Submission(
        submission_id=sub_id,
        team_id=team.team_id,
        attempt_number=1,
        status="in_progress",
        started_at=datetime.now(UTC),
        per_task_results=[],
    )
    db_session.add(sub)
    await db_session.commit()

    headers = {"Authorization": f"Bearer {token}"}

    # Fire two concurrent finalize requests
    resps = await asyncio.gather(
        client.post(f"/submission/{sub_id}/finalize", headers=headers),
        client.post(f"/submission/{sub_id}/finalize", headers=headers),
    )

    assert all(r.status_code == 200 for r in resps)
    for r in resps:
        data = r.json()
        assert data["submission_id"] == str(sub_id)
        assert data["status"] == "completed"

    # Database verification: submission is completed
    db_session.expire_all()
    sub_db = await db_session.get(Submission, sub_id)
    assert sub_db is not None
    assert sub_db.status == "completed"
