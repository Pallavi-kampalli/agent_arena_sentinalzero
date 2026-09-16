import uuid
from datetime import UTC, datetime, timedelta

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from agent_arena.config import get_config
from agent_arena.models.submission import Submission
from agent_arena.models.task import Task
from agent_arena.models.team import Team
from agent_arena.services.auth_service import hash_token
from agent_arena.services.settings_service import SettingsService
from conftest import generate_world


@pytest.fixture
def admin_headers():
    secret = get_config().ADMIN_PANEL_SECRET
    return {"X-Admin-Secret": secret}


@pytest.fixture
async def seeded_scoring_tasks(db_session: AsyncSession):
    settings = SettingsService(db_session)
    await settings.seed_defaults()
    await settings.set("hidden_task_count", 2)
    for i in range(2):
        t_id = f"TASK-SCOR-{i:03d}"
        world = generate_world(seed=7000 + i)
        task = Task(
            task_id=t_id,
            dataset="hidden",
            family="refund_request",
            variant="normal",
            input_payload={"customer_id": f"CUS-{i}", "customer_message": "Refund please"},
            world_state_seed=world,
            ground_truth={"expected_resolution": "refund", "must_escalate": False},
        )
        db_session.add(task)
    await db_session.commit()


@pytest.mark.asyncio
async def test_admin_leaderboard_4_tier_tiebreaks(client: AsyncClient, admin_headers, db_session: AsyncSession):
    """Leaderboard rigorously verifies 4-tier tiebreak order:
    1. aggregate_score DESC
    2. task_success DESC
    3. policy DESC
    4. earliest submission completed_at ASC
    """
    base_time = datetime(2026, 9, 15, 12, 0, 0, tzinfo=UTC)

    # Team 1: Score 0.90 -> Rank 1
    t1 = Team(team_id=uuid.uuid4(), team_name="Team1_TopScore", bearer_token_hash=hash_token("t1"), status="active")
    db_session.add(t1)
    s1 = Submission(
        submission_id=uuid.uuid4(),
        team_id=t1.team_id,
        attempt_number=1,
        status="completed",
        aggregate_score=0.90,
        breakdown={"dimensions": {"task_success": 0.90, "policy": 0.90}},
        completed_at=base_time,
    )
    db_session.add(s1)

    # Team 2: Score 0.80, task_success 0.85 -> Rank 2 (beats Team 3 on task_success)
    t2 = Team(team_id=uuid.uuid4(), team_name="Team2_TaskSuccess", bearer_token_hash=hash_token("t2"), status="active")
    db_session.add(t2)
    s2 = Submission(
        submission_id=uuid.uuid4(),
        team_id=t2.team_id,
        attempt_number=1,
        status="completed",
        aggregate_score=0.80,
        breakdown={"dimensions": {"task_success": 0.85, "policy": 0.70}},
        completed_at=base_time,
    )
    db_session.add(s2)

    # Team 3: Score 0.80, task_success 0.80, policy 0.90 -> Rank 3 (beats Team 4 on policy)
    t3 = Team(
        team_id=uuid.uuid4(), team_name="Team3_PolicyTiebreak", bearer_token_hash=hash_token("t3"), status="active"
    )
    db_session.add(t3)
    s3 = Submission(
        submission_id=uuid.uuid4(),
        team_id=t3.team_id,
        attempt_number=1,
        status="completed",
        aggregate_score=0.80,
        breakdown={"dimensions": {"task_success": 0.80, "policy": 0.90}},
        completed_at=base_time,
    )
    db_session.add(s3)

    # Team 4: Score 0.80, task_success 0.80, policy 0.80, completed earlier -> Rank 4 (beats Team 5 on time)
    t4 = Team(team_id=uuid.uuid4(), team_name="Team4_EarlyBird", bearer_token_hash=hash_token("t4"), status="active")
    db_session.add(t4)
    s4 = Submission(
        submission_id=uuid.uuid4(),
        team_id=t4.team_id,
        attempt_number=1,
        status="completed",
        aggregate_score=0.80,
        breakdown={"dimensions": {"task_success": 0.80, "policy": 0.80}},
        completed_at=base_time - timedelta(minutes=15),
    )
    db_session.add(s4)

    # Team 5: Score 0.80, task_success 0.80, policy 0.80, completed later -> Rank 5
    t5 = Team(team_id=uuid.uuid4(), team_name="Team5_LateBird", bearer_token_hash=hash_token("t5"), status="active")
    db_session.add(t5)
    s5 = Submission(
        submission_id=uuid.uuid4(),
        team_id=t5.team_id,
        attempt_number=1,
        status="completed",
        aggregate_score=0.80,
        breakdown={"dimensions": {"task_success": 0.80, "policy": 0.80}},
        completed_at=base_time + timedelta(minutes=15),
    )
    db_session.add(s5)

    await db_session.commit()

    resp = await client.get("/admin/leaderboard", headers=admin_headers)
    assert resp.status_code == 200
    data = resp.json()

    entries = data["leaderboard"]
    team_names_ranked = [e["team_name"] for e in entries]

    assert team_names_ranked == [
        "Team1_TopScore",
        "Team2_TaskSuccess",
        "Team3_PolicyTiebreak",
        "Team4_EarlyBird",
        "Team5_LateBird",
    ]

    for expected_rank, entry in enumerate(entries, start=1):
        assert entry["rank"] == expected_rank


@pytest.mark.asyncio
async def test_admin_leaderboard_disqualified_team_excluded(
    client: AsyncClient, admin_headers, db_session: AsyncSession
):
    """Disqualified teams must be completely excluded from the leaderboard."""
    dq_team = Team(
        team_id=uuid.uuid4(),
        team_name="CheaterTeam_DQ",
        bearer_token_hash=hash_token("dq"),
        status="disqualified",
    )
    db_session.add(dq_team)
    sub = Submission(
        submission_id=uuid.uuid4(),
        team_id=dq_team.team_id,
        attempt_number=1,
        status="completed",
        aggregate_score=0.99,
        breakdown={"dimensions": {"task_success": 0.99}},
        completed_at=datetime.now(UTC),
    )
    db_session.add(sub)
    await db_session.commit()

    resp = await client.get("/admin/leaderboard", headers=admin_headers)
    assert resp.status_code == 200
    entries = resp.json()["leaderboard"]
    assert "CheaterTeam_DQ" not in [e["team_name"] for e in entries]


@pytest.mark.asyncio
async def test_admin_leaderboard_csv_export(client: AsyncClient, admin_headers, db_session: AsyncSession):
    """Leaderboard CSV export returns text/csv format with canonical columns."""
    resp = await client.get("/admin/leaderboard/export", headers=admin_headers)
    assert resp.status_code == 200
    assert "text/csv" in resp.headers["content-type"]
    lines = resp.text.strip().split("\r\n") if "\r\n" in resp.text else resp.text.strip().split("\n")
    header = lines[0]
    assert "Rank" in header
    assert "Team Name" in header
    assert "Aggregate Score" in header


@pytest.mark.asyncio
async def test_admin_submissions_list_and_detail(client: AsyncClient, admin_headers, db_session: AsyncSession):
    """Admin can list and view submissions with filters."""
    team = Team(team_id=uuid.uuid4(), team_name="ListSubTeam", bearer_token_hash=hash_token("lst"), status="active")
    db_session.add(team)
    sub = Submission(
        submission_id=uuid.uuid4(),
        team_id=team.team_id,
        attempt_number=1,
        status="completed",
        aggregate_score=0.75,
        breakdown={"dimensions": {"task_success": 0.75}},
        started_at=datetime.now(UTC),
        completed_at=datetime.now(UTC),
    )
    db_session.add(sub)
    await db_session.commit()

    # List submissions by team_id
    list_resp = await client.get(f"/admin/submissions?team_id={team.team_id}", headers=admin_headers)
    assert list_resp.status_code == 200
    list_data = list_resp.json()
    assert list_data["total"] == 1
    assert list_data["submissions"][0]["submission_id"] == str(sub.submission_id)

    # View submission detail
    detail_resp = await client.get(f"/admin/submissions/{sub.submission_id}", headers=admin_headers)
    assert detail_resp.status_code == 200
    detail_data = detail_resp.json()
    assert detail_data["submission_id"] == str(sub.submission_id)
    assert detail_data["aggregate_score"] == 0.75


@pytest.mark.asyncio
async def test_admin_trigger_score_and_force_rescore(
    client: AsyncClient, admin_headers, db_session: AsyncSession, seeded_scoring_tasks
):
    """Admin can trigger scoring and force-rescore completed submissions."""
    team = Team(
        team_id=uuid.uuid4(), team_name="ScoreTriggerTeam", bearer_token_hash=hash_token("stt"), status="active"
    )
    db_session.add(team)
    sub = Submission(
        submission_id=uuid.uuid4(),
        team_id=team.team_id,
        attempt_number=1,
        status="completed",
        aggregate_score=None,
        breakdown=None,
        started_at=datetime.now(UTC),
        completed_at=datetime.now(UTC),
    )
    db_session.add(sub)
    await db_session.commit()

    # 1. Trigger initial score
    score_resp = await client.post(f"/admin/submissions/{sub.submission_id}/score", headers=admin_headers)
    assert score_resp.status_code == 200
    score_data = score_resp.json()
    assert score_data["submission_id"] == str(sub.submission_id)
    assert score_data["aggregate_score"] is not None
    assert score_data["rescore_applied"] is False

    # 2. Force rescore
    force_resp = await client.post(f"/admin/submissions/{sub.submission_id}/score?force=true", headers=admin_headers)
    assert force_resp.status_code == 200
    force_data = force_resp.json()
    assert force_data["rescore_applied"] is True
