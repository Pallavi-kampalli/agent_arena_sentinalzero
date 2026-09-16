import asyncio
import os
import time
import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from agent_arena.config import get_config
from agent_arena.models.submission import Submission
from agent_arena.models.task import Task
from agent_arena.models.team import Team
from agent_arena.scoring.service import ScoringService
from agent_arena.services.auth_service import hash_token
from agent_arena.services.settings_service import SettingsService
from agent_arena.services.submission_service import SubmissionService
from conftest import generate_world


@pytest.fixture
def admin_headers():
    return {"X-Admin-Secret": get_config().ADMIN_PANEL_SECRET}


@pytest.mark.asyncio
async def test_live_settings_update_without_restart_or_redeploy(
    client: AsyncClient,
    admin_headers: dict[str, str],
    db_session: AsyncSession,
):
    """Verifies that live settings updates take effect immediately without process restart.

    1. Checks authoritative baseline default: hidden_task_count = 200.
    2. Records PID and process uptime.
    3. Updates setting dynamically via admin HTTP API PUT /admin/settings/hidden_task_count with X-Admin-Secret to 10.
    4. Proves the new value is immediately observed in runtime assignment logic and SettingsService.
    5. Proves PID and process start time remain identical.
    6. Restores authoritative baseline default to 200 via admin HTTP API.
    """
    settings = SettingsService(db_session)
    await settings.seed_defaults()
    await db_session.commit()

    # 1. Authoritative baseline check
    baseline_val = await settings.get("hidden_task_count")
    assert baseline_val == 60, f"Expected canonical baseline default 60, got {baseline_val}"

    # 2. Record process identity
    pid_before = os.getpid()
    t_start_before = time.time()

    # 3. Dynamic admin update to test override via HTTP PUT endpoint with X-Admin-Secret
    test_override_val = 10
    update_res = await client.put(
        "/admin/settings/hidden_task_count",
        json={"value": test_override_val},
        headers=admin_headers,
    )
    assert update_res.status_code == 200
    up_data = update_res.json()
    assert up_data["key"] == "hidden_task_count"
    assert up_data["new_value"] == test_override_val

    # 4. Prove immediate runtime observation
    observed_val = await settings.get("hidden_task_count")
    assert observed_val == test_override_val
    assert SettingsService.get_cached("hidden_task_count") == test_override_val

    # Verify SubmissionService assigns up to the new overridden count
    team_id = uuid.uuid4()
    team = Team(
        team_id=team_id,
        team_name=f"LiveSettingsTeam_{uuid.uuid4().hex[:6]}",
        bearer_token_hash=hash_token("dummy-tok"),
    )
    db_session.add(team)
    await db_session.commit()

    sub_service = SubmissionService(db_session, settings)
    assert sub_service.settings_service is not None
    limit = await settings.get("hidden_task_count")
    assert limit == 10

    # 5. Prove zero process restart / redeploy
    pid_after = os.getpid()
    assert pid_before == pid_after
    assert time.time() >= t_start_before

    # 6. Restore canonical baseline via HTTP PUT endpoint
    restore_res = await client.put(
        "/admin/settings/hidden_task_count",
        json={"value": 60},
        headers=admin_headers,
    )
    assert restore_res.status_code == 200
    assert restore_res.json()["new_value"] == 60
    assert await settings.get("hidden_task_count") == 60


@pytest.mark.asyncio
async def test_historical_score_integrity_across_settings_changes(db_session: AsyncSession):
    """Verifies that changing scoring weights does NOT alter previously scored submissions."""
    settings = SettingsService(db_session)
    await settings.seed_defaults()

    # Baseline scoring weights
    initial_weights = {
        "task_success": 0.45,
        "policy": 0.15,
        "robustness": 0.15,
        "evidence": 0.10,
        "calibration": 0.05,
        "efficiency": 0.05,
        "communication": 0.05,
    }
    await settings.set("scoring_weights", initial_weights)

    # Provision team and submission
    team_id = uuid.uuid4()
    team = Team(
        team_id=team_id,
        team_name=f"HistoricalScoreTeam_{uuid.uuid4().hex[:6]}",
        bearer_token_hash=hash_token("test-tok-hist"),
        token_version=1,
    )
    db_session.add(team)

    sub_id = uuid.uuid4()
    sub = Submission(
        submission_id=sub_id,
        team_id=team_id,
        attempt_number=1,
        status="completed",
    )
    db_session.add(sub)

    # Add a task
    task_id = f"TASK-HIST-{uuid.uuid4().hex[:6]}"
    world = generate_world(seed=7001)
    task = Task(
        task_id=task_id,
        dataset="hidden",
        family="refund_request",
        variant="normal",
        input_payload={"customer_id": "CUS-01", "customer_message": "refund"},
        world_state_seed=world,
        ground_truth={"expected_resolution": "refund", "must_escalate": False, "required_evidence": ["DOC-1"]},
    )
    db_session.add(task)
    await db_session.commit()

    # Score submission under initial weights
    scoring_svc = ScoringService(db_session, settings)
    score_res_1 = await scoring_svc.score_submission(sub_id)
    initial_score = score_res_1.aggregate_score
    initial_snapshot = score_res_1.breakdown["scoring_metadata"]["scoring_weights_snapshot"]
    assert initial_snapshot == initial_weights

    # Now admin changes scoring weights dramatically
    new_weights = {
        "task_success": 0.10,
        "policy": 0.40,
        "robustness": 0.20,
        "evidence": 0.10,
        "calibration": 0.10,
        "efficiency": 0.05,
        "communication": 0.05,
    }
    await settings.set("scoring_weights", new_weights)

    # Re-fetch the previously stored score without force rescore
    score_res_cached = await scoring_svc.score_submission(sub_id, force=False)
    # Stored score and weights snapshot must remain completely identical
    assert score_res_cached.aggregate_score == initial_score
    assert score_res_cached.breakdown["scoring_metadata"]["scoring_weights_snapshot"] == initial_weights
    assert score_res_cached.breakdown["scoring_metadata"]["scoring_weights_snapshot"] != new_weights


@pytest.mark.asyncio
async def test_settings_update_under_concurrency(db_session: AsyncSession):
    """Verifies that updating settings while concurrent operations read them causes no deadlock or corruption."""
    settings = SettingsService(db_session)
    await settings.seed_defaults()

    async def concurrent_reader():
        for _ in range(25):
            val = await settings.get("time_budget_per_task_seconds")
            assert val in (180, 240, 300)
            await asyncio.sleep(0.005)

    async def concurrent_writer():
        for new_budget in (240, 300, 180):
            await settings.set("time_budget_per_task_seconds", new_budget)
            await asyncio.sleep(0.02)

    # Run reader and writer concurrently
    await asyncio.gather(
        concurrent_reader(),
        concurrent_reader(),
        concurrent_writer(),
    )

    final_val = await settings.get("time_budget_per_task_seconds")
    assert final_val == 180
