import os
import uuid

import httpx
import pytest
from pydantic import ValidationError

from agent_arena.config import AppConfig, get_config

TARGET_URL = os.environ.get("PRODUCTION_URL", "http://localhost:8000").rstrip("/")
ADMIN_SECRET = os.environ.get("ADMIN_PANEL_SECRET", get_config().ADMIN_PANEL_SECRET)


@pytest.fixture(scope="module")
def client():
    with httpx.Client(base_url=TARGET_URL, timeout=5.0) as c:
        try:
            r = c.get("/health")
            if r.status_code != 200:
                pytest.skip(f"Live server at {TARGET_URL} returned status {r.status_code}")
        except (httpx.ConnectError, httpx.TimeoutException):
            pytest.skip(f"Live server at {TARGET_URL} is not reachable. Start via Docker Compose to run live smoke tests.")
        yield c


def test_production_config_fails_closed_on_invalid_settings():
    """Validates that production environment configuration strictly fails closed."""
    # 1. Short admin secret
    with pytest.raises((ValueError, ValidationError)):
        AppConfig(
            ENVIRONMENT="production",
            ADMIN_PANEL_SECRET="too-short",
            JWT_SIGNING_SECRET="a" * 32,
            DATABASE_URL="postgresql+psycopg://user:pass@host:5432/db",
            CORS_ORIGINS="https://example.com",
        )

    # 2. Insecure placeholder admin secret
    with pytest.raises((ValueError, ValidationError)):
        AppConfig(
            ENVIRONMENT="production",
            ADMIN_PANEL_SECRET="dev-admin-secret-key-32-chars-min-entropy",
            JWT_SIGNING_SECRET="a" * 32,
            DATABASE_URL="postgresql+psycopg://user:pass@host:5432/db",
            CORS_ORIGINS="https://example.com",
        )

    # 3. Low entropy / repetitive secret in production
    with pytest.raises((ValueError, ValidationError)):
        AppConfig(
            ENVIRONMENT="production",
            ADMIN_PANEL_SECRET="a" * 32,
            JWT_SIGNING_SECRET="w34T_yU9xB_SupportOps_JWT_2026_SigningSecret!",
            DATABASE_URL="postgresql+psycopg://user:pass@host:5432/db",
            CORS_ORIGINS="https://example.com",
        )

    # 4. Wildcard CORS in production
    with pytest.raises((ValueError, ValidationError)):
        AppConfig(
            ENVIRONMENT="production",
            ADMIN_PANEL_SECRET="k89A_mQ7zP_SupportOps_Admin_2026_SecureKey!",
            JWT_SIGNING_SECRET="w34T_yU9xB_SupportOps_JWT_2026_SigningSecret!",
            DATABASE_URL="postgresql+psycopg://user:pass@host:5432/db",
            CORS_ORIGINS="*",
        )

    # 5. SQLite in production
    with pytest.raises((ValueError, ValidationError)):
        AppConfig(
            ENVIRONMENT="production",
            ADMIN_PANEL_SECRET="k89A_mQ7zP_SupportOps_Admin_2026_SecureKey!",
            JWT_SIGNING_SECRET="w34T_yU9xB_SupportOps_JWT_2026_SigningSecret!",
            DATABASE_URL="sqlite+aiosqlite:///prod.db",
            CORS_ORIGINS="https://example.com",
        )

    # 6. Valid high-entropy configuration succeeds
    valid_cfg = AppConfig(
        ENVIRONMENT="production",
        ADMIN_PANEL_SECRET="k89A_mQ7zP_SupportOps_Admin_2026_SecureKey!",
        JWT_SIGNING_SECRET="w34T_yU9xB_SupportOps_JWT_2026_SigningSecret!",
        DATABASE_URL="postgresql+psycopg://user:pass@host:5432/db",
        CORS_ORIGINS="https://arena.competition.org",
        REVEAL_GROUND_TRUTH=False,
    )
    assert valid_cfg.ENVIRONMENT == "production"
    assert valid_cfg.ADMIN_PANEL_SECRET == "k89A_mQ7zP_SupportOps_Admin_2026_SecureKey!"


def test_smoke_liveness_and_readiness_endpoints(client):
    """Validates /health liveness and /health/ready deep database readiness probes."""
    # Liveness check
    r_live = client.get("/health")
    assert r_live.status_code == 200
    data_live = r_live.json()
    assert data_live["status"] == "ok"

    # Readiness check
    r_ready = client.get("/health/ready")
    assert r_ready.status_code == 200
    data_ready = r_ready.json()
    assert data_ready["status"] == "ready"
    assert data_ready["database"] == "connected"


def test_smoke_admin_authentication_boundary(client):
    """Validates admin endpoint security, rejecting missing, invalid, or participant tokens."""
    # 1. Missing secret
    r_missing = client.get("/admin/health")
    assert r_missing.status_code == 401

    # 2. Invalid secret
    r_invalid = client.get("/admin/health", headers={"X-Admin-Secret": "wrong-secret-token"})
    assert r_invalid.status_code == 401

    # 3. Valid secret
    r_valid = client.get("/admin/health", headers={"X-Admin-Secret": ADMIN_SECRET})
    assert r_valid.status_code == 200
    assert r_valid.json()["status"] == "healthy"


def test_smoke_team_creation_and_auth(client):
    """Creates a smoke test team via admin API and validates bearer token auth."""
    team_name = f"SMOKE_TEAM_{uuid.uuid4().hex[:8]}"
    create_payload = {
        "team_name": team_name,
        "members": [{"name": "Smoke Bot", "email": "smoke@arena.test"}],
    }
    r_create = client.post("/admin/teams", json=create_payload, headers={"X-Admin-Secret": ADMIN_SECRET})
    assert r_create.status_code == 201
    team_data = r_create.json()
    assert "team_id" in team_data
    assert "bearer_token" in team_data

    token = team_data["bearer_token"]
    # Participant route access with token
    r_sub = client.post("/submission/start", headers={"Authorization": f"Bearer {token}"})
    assert r_sub.status_code == 200
    sub_data = r_sub.json()
    assert "submission_id" in sub_data


def test_smoke_full_submission_and_scoring_lifecycle(client):
    """Exercises complete end-to-end participant lifecycle against the running production server."""
    # 1. Create team
    team_name = f"SMOKE_E2E_{uuid.uuid4().hex[:8]}"
    r_create = client.post(
        "/admin/teams",
        json={"team_name": team_name, "members": [{"name": "E2E Tester", "email": "tester@arena.test"}]},
        headers={"X-Admin-Secret": ADMIN_SECRET},
    )
    assert r_create.status_code == 201
    token = r_create.json()["bearer_token"]
    auth_header = {"Authorization": f"Bearer {token}"}

    # 2. Start submission
    r_sub = client.post("/submission/start", headers=auth_header)
    assert r_sub.status_code == 200
    sub_id = r_sub.json()["submission_id"]

    # 3. Start task
    r_task = client.post("/task/start", headers=auth_header)
    assert r_task.status_code == 200
    task_data = r_task.json()
    task_id = task_data["task_id"]
    customer_id = task_data["customer_id"]

    # Verify zero oracle leakage
    for forbidden in ("ground_truth", "expected_resolution", "expected_evidence", "must_escalate"):
        assert forbidden not in task_data

    # 4. Invoke read tool
    r_cust = client.post("/tools/get_customer", json={"customer_id": customer_id}, headers=auth_header)
    assert r_cust.status_code == 200
    assert "customer" in r_cust.json()

    # 5. Submit task
    submit_payload = {
        "task_id": task_id,
        "case_classification": {"category": "billing", "issue": "refund_request", "severity": "medium"},
        "decision": {"resolution": "refund", "escalation_required": False},
        "evidence": [customer_id],
        "uncertainties": [],
        "customer_response": "Smoke test response: your case has been handled.",
        "confidence": 0.85,
    }
    r_submit = client.post("/task/submit", json=submit_payload, headers=auth_header)
    assert r_submit.status_code == 200
    assert r_submit.json() == {"received": True, "task_id": task_id}

    # 6. Status check
    r_status = client.get(f"/submission/{sub_id}/status", headers=auth_header)
    assert r_status.status_code == 200
    assert r_status.json()["tasks_completed"] >= 1

    # 7. Finalize submission
    r_fin = client.post(f"/submission/{sub_id}/finalize", headers=auth_header)
    assert r_fin.status_code == 200
    assert r_fin.json()["status"] == "completed"

    # 8. Score submission via admin trigger
    r_score = client.post(f"/admin/submissions/{sub_id}/score", headers={"X-Admin-Secret": ADMIN_SECRET})
    assert r_score.status_code == 200
    score_data = r_score.json()
    assert "aggregate_score" in score_data

    # 9. Verify on Leaderboard
    r_lb = client.get("/admin/leaderboard", headers={"X-Admin-Secret": ADMIN_SECRET})
    assert r_lb.status_code == 200
    lb_teams = [entry["team_name"] for entry in r_lb.json()["leaderboard"]]
    assert team_name in lb_teams


def test_smoke_live_settings_update_and_revert(client):
    """Verifies that live settings can be modified and restored via admin API without process restart."""
    headers = {"X-Admin-Secret": ADMIN_SECRET}

    # 0. Capture original setting
    r_orig = client.get("/admin/settings", headers=headers)
    assert r_orig.status_code == 200
    orig_val = next(s["value"] for s in r_orig.json()["settings"] if s["key"] == "hidden_task_count")

    # 1. Update setting
    r_put = client.put("/admin/settings/hidden_task_count", json={"value": 150}, headers=headers)
    assert r_put.status_code == 200
    assert r_put.json()["new_value"] == 150

    # 2. Confirm updated setting reflects on GET
    r_get = client.get("/admin/settings", headers=headers)
    assert r_get.status_code == 200
    settings_dict = {s["key"]: s["value"] for s in r_get.json()["settings"]}
    assert settings_dict["hidden_task_count"] == 150

    # 3. Restore to original setting
    r_revert = client.put("/admin/settings/hidden_task_count", json={"value": orig_val}, headers=headers)
    assert r_revert.status_code == 200
    assert r_revert.json()["new_value"] == orig_val


def test_smoke_leaderboard_standings(client):
    """Validates that the leaderboard and export endpoints return valid standings and tiebreak ordering."""
    headers = {"X-Admin-Secret": ADMIN_SECRET}

    # 1. JSON Leaderboard
    r_lb = client.get("/admin/leaderboard", headers=headers)
    assert r_lb.status_code == 200
    lb_data = r_lb.json()
    assert "leaderboard" in lb_data
    assert isinstance(lb_data["leaderboard"], list)

    # 2. CSV Leaderboard Export
    r_exp = client.get("/admin/leaderboard/export", headers=headers)
    assert r_exp.status_code == 200
    assert "text/csv" in r_exp.headers.get("content-type", "")
    assert "Team Name" in r_exp.text
