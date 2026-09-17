"""SentinelZero action tools tests.

Verifies the 4 action tool endpoints:
- /tools/quarantine_message
- /tools/allow_and_deliver
- /tools/apply_warning_banner
- /tools/escalate_to_tier2_soc
"""

import pytest
import sqlalchemy as sa
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from agent_arena.models.task_assignment import TaskAssignment
from agent_arena.models.tool_call_log import ToolCallLog
from agent_arena.services.auth_service import register_team
from agent_arena.services.dataset_service import DatasetService
from agent_arena.services.settings_service import SettingsService


@pytest.fixture
async def setup_action_world(client: AsyncClient, db_session: AsyncSession):
    """Sets up a team, seeded hidden tasks, starts a submission, and returns task_id + headers."""
    settings_service = SettingsService(db_session)
    await settings_service.seed_defaults()

    ds = DatasetService(db_session)
    await ds.generate_and_load_dataset(dataset_type="hidden", replace_existing=True)

    team, token = await register_team(
        session=db_session,
        team_name="ActionTesters",
        members=[{"name": "Carol"}],
    )

    headers = {"Authorization": f"Bearer {token}"}
    start_resp = await client.post("/submission/start", headers=headers)
    assert start_resp.status_code == 200

    task_id = start_resp.json()["tasks"][0]["task_id"]
    all_tasks = start_resp.json()["tasks"]
    task_idx = task_id.replace("TASK-HIDDEN-", "")
    message_id = f"MSG-HIDDEN-{task_idx}"
    sub_id = start_resp.json()["submission_id"]

    headers["X-Task-ID"] = task_id

    return {
        "team": team,
        "token": token,
        "task_id": task_id,
        "message_id": message_id,
        "sub_id": sub_id,
        "headers": headers,
        "settings_service": settings_service,
    }


# =============================================================================
# Quarantine Message Tests
# =============================================================================


@pytest.mark.asyncio
async def test_quarantine_message_success_and_state_mutation(
    client: AsyncClient, setup_action_world, db_session: AsyncSession
):
    """Verify quarantine_message succeeds, mutates world state delivery_status, and records tool log."""
    message_id = setup_action_world["message_id"]
    headers = setup_action_world["headers"]

    resp = await client.post(
        "/tools/quarantine_message",
        json={"message_id": message_id, "reason": "Phishing indicators detected: SPF fail and malicious domain"},
        headers=headers,
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "quarantined"
    assert data["message_id"] == message_id

    # Verify tool log recorded with no enforcement rejection
    log = (
        await db_session.execute(
            sa.select(ToolCallLog)
            .where(ToolCallLog.tool_name == "quarantine_message")
            .order_by(ToolCallLog.id.desc())
        )
    ).scalars().first()
    assert log is not None
    assert log.was_enforcement_rejection is False
    assert log.latency_ms > 0


@pytest.mark.asyncio
async def test_allow_and_deliver_success(client: AsyncClient, setup_action_world):
    """Verify allow_and_deliver succeeds and returns delivered status."""
    message_id = setup_action_world["message_id"]
    headers = setup_action_world["headers"]

    resp = await client.post(
        "/tools/allow_and_deliver",
        json={"message_id": message_id, "reason": "All indicators verified clean. SPF pass, DKIM pass."},
        headers=headers,
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "delivered"


@pytest.mark.asyncio
async def test_apply_warning_banner_success(client: AsyncClient, setup_action_world):
    """Verify apply_warning_banner succeeds and returns warning_applied status."""
    message_id = setup_action_world["message_id"]
    headers = setup_action_world["headers"]

    resp = await client.post(
        "/tools/apply_warning_banner",
        json={"message_id": message_id, "banner_type": "EXTERNAL_SENDER", "reason": "External sender requiring attention"},
        headers=headers,
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "warning_applied"


# =============================================================================
# Escalate to Tier-2 SOC Tests (Evidence Grounding)
# =============================================================================


@pytest.mark.asyncio
async def test_escalate_to_tier2_soc_grounded_vs_ungrounded(client: AsyncClient, setup_action_world):
    """Verify SOC escalation succeeds ONLY when reason cites *retrieved* evidence IDs."""
    message_id = setup_action_world["message_id"]
    headers = setup_action_world["headers"]

    # 1. Ungrounded escalation (reason doesn't cite any retrieved evidence ID)
    resp_ungrounded = await client.post(
        "/tools/escalate_to_tier2_soc",
        json={"message_id": message_id, "reason": "The email looks suspicious to me personally"},
        headers=headers,
    )
    assert resp_ungrounded.status_code == 200
    assert resp_ungrounded.json()["error"] == "INVALID_ESCALATION"
    assert resp_ungrounded.json()["reason"] == "reason_not_grounded"

    # 2. Retrieve email headers first to get evidence
    h_resp = await client.post("/tools/get_email_headers", json={"message_id": message_id}, headers=headers)
    assert h_resp.status_code == 200

    # 3. Grounded escalation citing the retrieved message_id
    resp_grounded = await client.post(
        "/tools/escalate_to_tier2_soc",
        json={
            "message_id": message_id,
            "reason": f"Advanced phishing campaign detected on {message_id}. SPF and DKIM failures. Requires human SOC review.",
        },
        headers=headers,
    )
    assert resp_grounded.status_code == 200
    assert resp_grounded.json()["status"] == "escalated_to_soc"


@pytest.mark.asyncio
async def test_action_tool_zero_mutation_on_rejection(client: AsyncClient, setup_action_world, db_session: AsyncSession):
    """Verify INVALID_ESCALATION rejection leaves world state completely unchanged."""
    message_id = setup_action_world["message_id"]
    headers = setup_action_world["headers"]
    team = setup_action_world["team"]

    # Trigger assignment creation by calling a read tool first
    await client.post("/tools/lookup_directory", json={"identifier": "alice@sentinel-acme.edu"}, headers=headers)

    # Get assignment — query most recent for this team
    assignment_before = (
        await db_session.execute(
            sa.select(TaskAssignment)
            .where(TaskAssignment.team_id == team.team_id)
            .order_by(TaskAssignment.id.desc())
        )
    ).scalars().first()
    assert assignment_before is not None, "Assignment not created even after tool call"
    import copy as _copy
    before_state = _copy.deepcopy(assignment_before.world_runtime_state)

    # Attempt ungrounded escalation
    resp = await client.post(
        "/tools/escalate_to_tier2_soc",
        json={"message_id": message_id, "reason": "Just a hunch, no evidence"},
        headers=headers,
    )
    assert resp.status_code == 200
    assert resp.json()["error"] == "INVALID_ESCALATION"

    # Verify world state was NOT mutated
    # Capture team_id before expire_all to avoid lazy-load
    team_id = team.team_id
    db_session.expire_all()
    assignment_after = (
        await db_session.execute(
            sa.select(TaskAssignment)
            .where(TaskAssignment.team_id == team_id)
            .order_by(TaskAssignment.id.desc())
        )
    ).scalars().first()
    after_state = assignment_after.world_runtime_state
    assert before_state == after_state


@pytest.mark.asyncio
async def test_enforcement_rejection_logged_correctly(client: AsyncClient, setup_action_world, db_session: AsyncSession):
    """Verify ungrounded escalation is logged as enforcement rejection in tool_call_log."""
    message_id = setup_action_world["message_id"]
    headers = setup_action_world["headers"]

    await client.post(
        "/tools/escalate_to_tier2_soc",
        json={"message_id": message_id, "reason": "No evidence cited at all"},
        headers=headers,
    )

    log = (
        await db_session.execute(
            sa.select(ToolCallLog)
            .where(ToolCallLog.tool_name == "escalate_to_tier2_soc")
            .order_by(ToolCallLog.id.desc())
        )
    ).scalars().first()
    assert log is not None
    assert log.was_enforcement_rejection is True


@pytest.mark.asyncio
async def test_no_active_task_action_returns_404(client: AsyncClient, db_session: AsyncSession):
    """Verify action tool call on a team with no active assignment returns 404 NO_ACTIVE_TASK."""
    settings_service = SettingsService(db_session)
    await settings_service.seed_defaults()

    _, token = await register_team(
        session=db_session,
        team_name="NoTaskTeam",
        members=[{"name": "Dave"}],
    )
    headers = {"Authorization": f"Bearer {token}"}

    resp = await client.post(
        "/tools/quarantine_message",
        json={"message_id": "MSG-HIDDEN-001", "reason": "Test"},
        headers=headers,
    )
    assert resp.status_code == 404
    assert resp.json()["detail"]["error"] == "NO_ACTIVE_TASK"
