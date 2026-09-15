import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from agent_arena.config import get_config
from agent_arena.services.settings_service import SettingsService


@pytest.fixture
def admin_headers():
    secret = get_config().ADMIN_PANEL_SECRET
    return {"X-Admin-Secret": secret}


@pytest.fixture
async def seeded_settings(db_session: AsyncSession):
    settings = SettingsService(db_session)
    await settings.seed_defaults()
    await db_session.commit()


@pytest.mark.asyncio
async def test_admin_get_settings(client: AsyncClient, admin_headers, seeded_settings):
    """Admin can fetch all system settings and defaults."""
    resp = await client.get("/admin/settings", headers=admin_headers)
    assert resp.status_code == 200
    data = resp.json()
    assert "settings" in data
    keys = [s["key"] for s in data["settings"]]
    assert "competition_phase" in keys
    assert "scoring_weights" in keys
    assert "submission_limit_per_team" in keys


@pytest.mark.asyncio
async def test_admin_update_setting_and_audit_log(client: AsyncClient, admin_headers, seeded_settings):
    """Updating a setting creates an audit log entry and updates the value."""
    # Update submission_limit_per_team to 10
    update_resp = await client.put(
        "/admin/settings/submission_limit_per_team",
        json={"value": 10},
        headers=admin_headers,
    )
    assert update_resp.status_code == 200
    up_data = update_resp.json()
    assert up_data["key"] == "submission_limit_per_team"
    assert up_data["old_value"] == 5
    assert up_data["new_value"] == 10

    # Verify audit log contains entry
    audit_resp = await client.get(
        "/admin/settings/audit-log?key=submission_limit_per_team",
        headers=admin_headers,
    )
    assert audit_resp.status_code == 200
    audit_data = audit_resp.json()
    assert audit_data["total"] >= 1
    latest = audit_data["audit_logs"][0]
    assert latest["key"] == "submission_limit_per_team"
    assert latest["old_value"] == 5
    assert latest["new_value"] == 10
    assert latest["changed_by"] == "admin"


@pytest.mark.asyncio
async def test_admin_update_scoring_weights_validation(client: AsyncClient, admin_headers, seeded_settings):
    """Scoring weights must sum to 1.0 and contain canonical dimensions."""
    # Invalid: weights sum to 0.8 != 1.0
    bad_weights = {
        "task_success": 0.2,
        "policy": 0.1,
        "robustness": 0.1,
        "evidence": 0.1,
        "calibration": 0.1,
        "efficiency": 0.1,
        "communication": 0.1,
    }
    resp = await client.put(
        "/admin/settings/scoring_weights",
        json={"value": bad_weights},
        headers=admin_headers,
    )
    assert resp.status_code == 400
    assert resp.json()["detail"]["error"] in ("INVALID_WEIGHTS", "INVALID_SETTING_VALUE")

    # Valid: weights sum to 1.0
    good_weights = {
        "task_success": 0.40,
        "policy": 0.10,
        "robustness": 0.10,
        "evidence": 0.10,
        "calibration": 0.10,
        "efficiency": 0.10,
        "communication": 0.10,
    }
    good_resp = await client.put(
        "/admin/settings/scoring_weights",
        json={"value": good_weights},
        headers=admin_headers,
    )
    assert good_resp.status_code == 200
    assert good_resp.json()["new_value"]["task_success"] == 0.40


@pytest.mark.asyncio
async def test_admin_competition_phase_transitions(client: AsyncClient, admin_headers, seeded_settings):
    """Competition phase state machine enforces forward-only transitions."""
    # Check initial phase
    resp = await client.get("/admin/competition/phase", headers=admin_headers)
    assert resp.status_code == 200
    assert resp.json()["current_phase"] == "registration"
    assert "build" in resp.json()["allowed_next_phases"]

    # Valid forward transition: registration -> build
    trans_resp = await client.post(
        "/admin/competition/phase",
        json={"new_phase": "build"},
        headers=admin_headers,
    )
    assert trans_resp.status_code == 200
    assert trans_resp.json()["current_phase"] == "build"

    # Invalid backward transition: build -> registration
    back_resp = await client.post(
        "/admin/competition/phase",
        json={"new_phase": "registration"},
        headers=admin_headers,
    )
    assert back_resp.status_code == 400
    assert back_resp.json()["detail"]["error"] == "INVALID_PHASE_TRANSITION"

    # Valid forward sequence: build -> frozen -> evaluating -> results_published
    for next_p in ["frozen", "evaluating", "results_published"]:
        step_resp = await client.post(
            "/admin/competition/phase",
            json={"new_phase": next_p},
            headers=admin_headers,
        )
        assert step_resp.status_code == 200
        assert step_resp.json()["current_phase"] == next_p
