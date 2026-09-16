import uuid
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Response
from fastapi.responses import HTMLResponse
from sqlalchemy.ext.asyncio import AsyncSession

from agent_arena.api.deps import AdminUser, get_current_admin, get_db_session, get_settings_service
from agent_arena.schemas.admin import (
    AdminAuditLogListResponse,
    AdminHealthResponse,
    AdminLeaderboardResponse,
    AdminPhaseResponse,
    AdminPhaseTransitionRequest,
    AdminScoreTriggerResponse,
    AdminSettingsResponse,
    AdminSettingUpdateRequest,
    AdminSettingUpdateResponse,
    AdminSubmissionDetailResponse,
    AdminSubmissionListResponse,
    AdminTeamBulkImportRequest,
    AdminTeamBulkImportResponse,
    AdminTeamCreateRequest,
    AdminTeamCreateResponse,
    AdminTeamDetailResponse,
    AdminTeamListResponse,
    AdminTeamStatusRequest,
    AdminTeamUpdateRequest,
    AdminTokenRegenerateResponse,
    AdminToolLogListResponse,
)
from agent_arena.services.admin_service import AdminService
from agent_arena.services.settings_service import SettingsService

router = APIRouter(prefix="/admin", tags=["Admin"])


# ------------------------------------------------------------------------------
# 1. Health & Operations Overview
# ------------------------------------------------------------------------------


@router.get("/health", response_model=AdminHealthResponse)
async def get_admin_health(
    admin: Annotated[AdminUser, Depends(get_current_admin)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
    settings_service: Annotated[SettingsService, Depends(get_settings_service)],
) -> AdminHealthResponse:
    """Returns operational system health, task-pool capacity, and latency metrics."""
    service = AdminService(session, settings_service)
    return await service.get_system_health()


# ------------------------------------------------------------------------------
# 2. Team Management
# ------------------------------------------------------------------------------


@router.get("/teams", response_model=AdminTeamListResponse)
async def list_teams(
    admin: Annotated[AdminUser, Depends(get_current_admin)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
    settings_service: Annotated[SettingsService, Depends(get_settings_service)],
    q: str | None = Query(None, description="Search term for team name"),
    search: str | None = Query(None, description="Search term for team name (alias)"),
    status: str | None = Query(None, description="Filter by status (active, suspended, disqualified)"),
    offset: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
) -> AdminTeamListResponse:
    """Lists teams with search, status filtering, and pagination."""
    service = AdminService(session, settings_service)
    teams, total = await service.list_teams(search=(q or search), status_filter=status, offset=offset, limit=limit)
    return AdminTeamListResponse(teams=teams, total=total, offset=offset, limit=limit)


@router.post("/teams", response_model=AdminTeamCreateResponse, status_code=201)
async def create_team(
    req: AdminTeamCreateRequest,
    admin: Annotated[AdminUser, Depends(get_current_admin)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
    settings_service: Annotated[SettingsService, Depends(get_settings_service)],
) -> AdminTeamCreateResponse:
    """Registers a new team and returns raw token and starter-kit .env snippet."""
    service = AdminService(session, settings_service)
    return await service.create_team(req)


@router.post("/teams/bulk-import", response_model=AdminTeamBulkImportResponse, status_code=201)
async def bulk_import_teams(
    req: AdminTeamBulkImportRequest,
    admin: Annotated[AdminUser, Depends(get_current_admin)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
    settings_service: Annotated[SettingsService, Depends(get_settings_service)],
) -> AdminTeamBulkImportResponse:
    """Imports teams in bulk from CSV content or structured team list."""
    service = AdminService(session, settings_service)
    return await service.bulk_import_teams(req)


@router.get("/teams/{team_id}", response_model=AdminTeamDetailResponse)
async def get_team_detail(
    team_id: uuid.UUID,
    admin: Annotated[AdminUser, Depends(get_current_admin)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
    settings_service: Annotated[SettingsService, Depends(get_settings_service)],
) -> AdminTeamDetailResponse:
    """Retrieves full profile, status, and submission records for a team."""
    service = AdminService(session, settings_service)
    return await service.get_team_detail(team_id)


@router.patch("/teams/{team_id}", response_model=AdminTeamDetailResponse)
async def update_team(
    team_id: uuid.UUID,
    req: AdminTeamUpdateRequest,
    admin: Annotated[AdminUser, Depends(get_current_admin)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
    settings_service: Annotated[SettingsService, Depends(get_settings_service)],
) -> AdminTeamDetailResponse:
    """Updates team metadata (display name, members, repository link)."""
    service = AdminService(session, settings_service)
    return await service.update_team(team_id, req)


@router.post("/teams/{team_id}/status", response_model=AdminTeamDetailResponse)
@router.put("/teams/{team_id}/status", response_model=AdminTeamDetailResponse)
async def update_team_status(
    team_id: uuid.UUID,
    req: AdminTeamStatusRequest,
    admin: Annotated[AdminUser, Depends(get_current_admin)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
    settings_service: Annotated[SettingsService, Depends(get_settings_service)],
) -> AdminTeamDetailResponse:
    """Changes team status (active, suspended, disqualified)."""
    service = AdminService(session, settings_service)
    return await service.update_team_status(team_id, req.status, actor=admin.username)


@router.post("/teams/{team_id}/regenerate-token", response_model=AdminTokenRegenerateResponse)
@router.post("/teams/{team_id}/token", response_model=AdminTokenRegenerateResponse)
async def regenerate_team_token(
    team_id: uuid.UUID,
    admin: Annotated[AdminUser, Depends(get_current_admin)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
    settings_service: Annotated[SettingsService, Depends(get_settings_service)],
) -> AdminTokenRegenerateResponse:
    """Atomically bumps token_version and invalidates prior tokens immediately."""
    service = AdminService(session, settings_service)
    return await service.regenerate_team_token(team_id, actor=admin.username)


# ------------------------------------------------------------------------------
# 3. Settings & Competition Lifecycle
# ------------------------------------------------------------------------------


@router.get("/settings", response_model=AdminSettingsResponse)
async def get_all_settings(
    admin: Annotated[AdminUser, Depends(get_current_admin)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
    settings_service: Annotated[SettingsService, Depends(get_settings_service)],
) -> AdminSettingsResponse:
    """Lists all runtime competition settings."""
    service = AdminService(session, settings_service)
    return await service.get_all_settings()


@router.get("/settings/audit-logs", response_model=AdminAuditLogListResponse)
@router.get("/settings/audit-log", response_model=AdminAuditLogListResponse)
async def get_audit_logs(
    admin: Annotated[AdminUser, Depends(get_current_admin)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
    settings_service: Annotated[SettingsService, Depends(get_settings_service)],
    key: str | None = Query(None, description="Filter audit logs by setting key"),
    offset: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
) -> AdminAuditLogListResponse:
    """Queries immutable settings audit trail."""
    service = AdminService(session, settings_service)
    logs, total = await service.get_audit_logs(key=key, offset=offset, limit=limit)
    return AdminAuditLogListResponse(audit_logs=logs, total=total, offset=offset, limit=limit)


@router.put("/settings/{key}", response_model=AdminSettingUpdateResponse)
async def update_setting(
    key: str,
    req: AdminSettingUpdateRequest,
    admin: Annotated[AdminUser, Depends(get_current_admin)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
    settings_service: Annotated[SettingsService, Depends(get_settings_service)],
) -> AdminSettingUpdateResponse:
    """Updates a setting value with validation and writes an audit log entry."""
    service = AdminService(session, settings_service)
    return await service.update_setting(key, req.value, actor=admin.username)


@router.get("/competition/phase", response_model=AdminPhaseResponse)
async def get_competition_phase(
    admin: Annotated[AdminUser, Depends(get_current_admin)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
    settings_service: Annotated[SettingsService, Depends(get_settings_service)],
) -> AdminPhaseResponse:
    """Returns current competition phase and permitted forward transitions."""
    service = AdminService(session, settings_service)
    return await service.get_competition_phase()


@router.post("/competition/phase", response_model=AdminPhaseResponse)
async def transition_competition_phase(
    req: AdminPhaseTransitionRequest,
    admin: Annotated[AdminUser, Depends(get_current_admin)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
    settings_service: Annotated[SettingsService, Depends(get_settings_service)],
) -> AdminPhaseResponse:
    """Advances competition phase forward."""
    service = AdminService(session, settings_service)
    return await service.transition_competition_phase(req.new_phase, actor=admin.username)


# ------------------------------------------------------------------------------
# 4. Live Leaderboard & Submissions
# ------------------------------------------------------------------------------


@router.get("/leaderboard")
async def get_leaderboard(
    admin: Annotated[AdminUser, Depends(get_current_admin)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
    settings_service: Annotated[SettingsService, Depends(get_settings_service)],
    mode: str = Query("best", pattern="^(best|latest)$", description="Submission view mode"),
    export: str | None = Query(None, description="Set to 'csv' for file download"),
) -> Response:
    """Computes live leaderboard with tiebreak ranking and optional CSV export."""
    service = AdminService(session, settings_service)
    if export == "csv":
        csv_data = await service.export_leaderboard_csv(mode=mode)
        filename = f"leaderboard_{mode}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
        return Response(
            content=csv_data,
            media_type="text/csv",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )

    entries, tiebreaks = await service.get_leaderboard(mode=mode)
    return AdminLeaderboardResponse(leaderboard=entries, mode=mode, tiebreak_order=tiebreaks)  # type: ignore[return-value]


@router.get("/leaderboard/export")
async def export_leaderboard(
    admin: Annotated[AdminUser, Depends(get_current_admin)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
    settings_service: Annotated[SettingsService, Depends(get_settings_service)],
    mode: str = Query("best", pattern="^(best|latest)$", description="Submission view mode"),
) -> Response:
    """Exports live leaderboard as an RFC 4180 compliant CSV download."""
    service = AdminService(session, settings_service)
    csv_data = await service.export_leaderboard_csv(mode=mode)
    filename = f"leaderboard_{mode}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
    return Response(
        content=csv_data,
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/submissions", response_model=AdminSubmissionListResponse)
async def list_submissions(
    admin: Annotated[AdminUser, Depends(get_current_admin)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
    settings_service: Annotated[SettingsService, Depends(get_settings_service)],
    team_id: uuid.UUID | None = Query(None, description="Filter by team ID"),
    status: str | None = Query(None, description="Filter by status (in_progress, completed, expired)"),
    offset: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
) -> AdminSubmissionListResponse:
    """Lists submissions with team and status filtering."""
    service = AdminService(session, settings_service)
    subs, total = await service.list_submissions(team_id=team_id, status_filter=status, offset=offset, limit=limit)
    return AdminSubmissionListResponse(submissions=subs, total=total, offset=offset, limit=limit)


@router.get("/submissions/{submission_id}", response_model=AdminSubmissionDetailResponse)
async def get_submission_detail(
    submission_id: uuid.UUID,
    admin: Annotated[AdminUser, Depends(get_current_admin)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
    settings_service: Annotated[SettingsService, Depends(get_settings_service)],
) -> AdminSubmissionDetailResponse:
    """Retrieves full submission breakdown and task-level audit evaluation results."""
    service = AdminService(session, settings_service)
    return await service.get_submission_detail(submission_id)


@router.post("/submissions/{submission_id}/score", response_model=AdminScoreTriggerResponse)
async def trigger_submission_scoring(
    submission_id: uuid.UUID,
    admin: Annotated[AdminUser, Depends(get_current_admin)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
    settings_service: Annotated[SettingsService, Depends(get_settings_service)],
    force: bool = Query(False, description="Set true to force re-evaluation of already scored submissions"),
) -> AdminScoreTriggerResponse:
    """Triggers scoring or force-rescoring on a closed submission via ScoringService."""
    service = AdminService(session, settings_service)
    return await service.trigger_submission_scoring(submission_id, force=force, actor=admin.username)


# ------------------------------------------------------------------------------
# 5. Monitoring & Tool Logs
# ------------------------------------------------------------------------------


@router.get("/monitoring/tool-logs", response_model=AdminToolLogListResponse)
@router.get("/tool-logs", response_model=AdminToolLogListResponse)
async def get_tool_call_logs(
    admin: Annotated[AdminUser, Depends(get_current_admin)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
    settings_service: Annotated[SettingsService, Depends(get_settings_service)],
    team_id: uuid.UUID | None = Query(None, description="Filter logs by team ID"),
    task_id: str | None = Query(None, description="Filter logs by task ID"),
    tool_name: str | None = Query(None, description="Filter logs by tool name"),
    rejections_only: bool = Query(False, description="Only show enforcement rejections"),
    offset: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
) -> AdminToolLogListResponse:
    """Queries detailed tool execution logs for dispute resolution."""
    service = AdminService(session, settings_service)
    logs, total = await service.get_tool_call_logs(
        team_id=team_id,
        task_id=task_id,
        tool_name=tool_name,
        rejections_only=rejections_only,
        offset=offset,
        limit=limit,
    )
    return AdminToolLogListResponse(logs=logs, total=total, offset=offset, limit=limit)


# ------------------------------------------------------------------------------
# 6. Operational Dashboard UI
# ------------------------------------------------------------------------------


DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Agent Arena — Admin Control Plane</title>
  <style>
    :root {
      --bg: #0f172a; --panel: #1e293b; --border: #334155;
      --text: #f8fafc; --muted: #94a3b8; --primary: #38bdf8;
      --success: #4ade80; --warning: #facc15; --danger: #f87171;
    }
    * { box-sizing: border-box; margin: 0; padding: 0; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; }
    body { background: var(--bg); color: var(--text); padding: 24px; }
    header { display: flex; justify-content: space-between; align-items: center; border-bottom: 1px solid var(--border); padding-bottom: 16px; margin-bottom: 24px; }
    h1 { font-size: 20px; font-weight: 700; color: var(--primary); }
    .badge { padding: 4px 10px; border-radius: 9999px; font-size: 12px; font-weight: 600; text-transform: uppercase; }
    .badge-healthy { background: rgba(74, 222, 128, 0.2); color: var(--success); }
    .nav-tabs { display: flex; gap: 8px; margin-bottom: 20px; border-bottom: 1px solid var(--border); }
    .nav-tab { padding: 10px 18px; background: transparent; border: none; color: var(--muted); cursor: pointer; font-size: 14px; font-weight: 600; }
    .nav-tab.active { color: var(--primary); border-bottom: 2px solid var(--primary); }
    .tab-content { display: none; }
    .tab-content.active { display: block; }
    .panel { background: var(--panel); border: 1px solid var(--border); border-radius: 8px; padding: 20px; margin-bottom: 20px; }
    .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 16px; }
    .stat-card { background: rgba(15, 23, 42, 0.6); padding: 16px; border-radius: 6px; border: 1px solid var(--border); }
    .stat-val { font-size: 24px; font-weight: 700; color: var(--text); margin-top: 4px; }
    .stat-label { font-size: 12px; color: var(--muted); text-transform: uppercase; }
    table { width: 100%; border-collapse: collapse; margin-top: 12px; font-size: 13px; }
    th, td { text-align: left; padding: 10px 12px; border-bottom: 1px solid var(--border); }
    th { color: var(--muted); font-weight: 600; }
    button { background: var(--primary); color: #0f172a; border: none; padding: 8px 14px; border-radius: 4px; font-size: 13px; font-weight: 600; cursor: pointer; }
    button.btn-danger { background: var(--danger); color: #fff; }
    button.btn-secondary { background: var(--border); color: var(--text); }
    input, select { background: #0f172a; border: 1px solid var(--border); color: #fff; padding: 8px 12px; border-radius: 4px; font-size: 13px; }
    .controls { display: flex; gap: 12px; margin-bottom: 16px; align-items: center; }
    .modal-backdrop { position: fixed; inset: 0; background: rgba(0,0,0,0.85); display: none; align-items: center; justify-content: center; z-index: 1000; }
    .modal-box { background: var(--panel); padding: 24px; border-radius: 8px; border: 1px solid var(--border); width: 440px; max-width: 90vw; }
    .modal-box.wide { width: 680px; }
    .form-group { margin-bottom: 14px; text-align: left; }
    .form-group label { display: block; font-size: 12px; color: var(--muted); margin-bottom: 5px; font-weight: 600; }
    .form-group input, .form-group textarea { width: 100%; box-sizing: border-box; }
    .modal-actions { display: flex; justify-content: flex-end; gap: 10px; margin-top: 18px; }
    pre { background: #0f172a; padding: 12px; border-radius: 4px; font-size: 12px; overflow-x: auto; color: var(--success); }
  </style>
</head>
<body>
  <!-- Admin Authentication Modal -->
  <div id="auth-modal" class="modal-backdrop">
    <div class="modal-box">
      <h2 style="font-size:16px;margin-bottom:12px;">Admin Authentication</h2>
      <p style="font-size:12px;color:var(--muted);margin-bottom:12px;">Enter the ADMIN_PANEL_SECRET to unlock the control plane.</p>
      <input id="secret-input" type="password" style="width:100%;margin-bottom:12px;" placeholder="Admin Secret">
      <button style="width:100%;" onclick="authenticate()">Unlock Dashboard</button>
    </div>
  </div>

  <!-- Team Registration Modal -->
  <div id="team-modal" class="modal-backdrop">
    <div class="modal-box">
      <h2 style="font-size:16px;margin-bottom:8px;">Register New Competition Team</h2>
      <p style="font-size:12px;color:var(--muted);margin-bottom:16px;">Create credentials for a participating team in the live arena.</p>
      <div class="form-group">
        <label>Team Name *</label>
        <input id="create-team-name" type="text" placeholder="e.g. Apex-Agents" required>
      </div>
      <div class="form-group">
        <label>Members (comma-separated names/emails)</label>
        <input id="create-team-members" type="text" placeholder="e.g. Alice &lt;alice@example.com&gt;, Bob">
      </div>
      <div class="form-group">
        <label>GitHub Repository URL (optional)</label>
        <input id="create-team-github" type="url" placeholder="https://github.com/team/agent">
      </div>
      <div id="team-modal-error" style="color:var(--danger);font-size:12px;display:none;margin-bottom:12px;"></div>
      <div class="modal-actions">
        <button class="btn-secondary" onclick="closeCreateTeamModal()">Cancel</button>
        <button onclick="submitCreateTeam()">Register Team</button>
      </div>
    </div>
  </div>

  <!-- Token / Environment Snippet Modal -->
  <div id="token-modal" class="modal-backdrop">
    <div class="modal-box wide">
      <h2 style="font-size:16px;margin-bottom:8px;" id="token-modal-title">Team Credentials</h2>
      <p style="font-size:12px;color:var(--muted);margin-bottom:14px;">Copy this configuration snippet directly into your team's <code>.env</code> file:</p>
      <pre id="token-modal-snippet" style="max-height:260px;"></pre>
      <div class="modal-actions">
        <button id="copy-token-btn" onclick="copyTokenSnippet()">📋 Copy to Clipboard</button>
        <button class="btn-secondary" onclick="closeTokenModal()">Close</button>
      </div>
    </div>
  </div>

  <!-- Submission Detail Modal -->
  <div id="submission-modal" class="modal-backdrop">
    <div class="modal-box wide">
      <h2 style="font-size:16px;margin-bottom:8px;" id="sub-modal-title">Submission Inspection</h2>
      <div id="sub-modal-content" style="font-size:13px;max-height:420px;overflow-y:auto;"></div>
      <div class="modal-actions">
        <button class="btn-secondary" onclick="closeSubmissionModal()">Close</button>
      </div>
    </div>
  </div>

  <header>
    <div>
      <h1>Agent Arena — Admin Control Plane</h1>
      <span style="font-size:12px;color:var(--muted)">Competition Operations & Monitoring</span>
    </div>
    <div style="display:flex;gap:12px;align-items:center;">
      <span id="system-badge" class="badge badge-healthy">System: Connecting...</span>
      <span id="phase-badge" class="badge" style="background:rgba(56,189,248,0.2);color:var(--primary)">Phase: ...</span>
    </div>
  </header>

  <div class="nav-tabs">
    <button class="nav-tab active" onclick="switchTab('overview')">Overview & Health</button>
    <button class="nav-tab" onclick="switchTab('teams')">Teams Management</button>
    <button class="nav-tab" onclick="switchTab('settings')">Settings & Audit</button>
    <button class="nav-tab" onclick="switchTab('leaderboard')">Live Leaderboard</button>
    <button class="nav-tab" onclick="switchTab('submissions')">Submissions & Scoring</button>
    <button class="nav-tab" onclick="switchTab('logs')">Tool Logs</button>
  </div>

  <!-- Overview Tab -->
  <div id="tab-overview" class="tab-content active">
    <div class="panel">
      <h2 style="font-size:16px;margin-bottom:16px;">System Health & Capacity</h2>
      <div class="grid">
        <div class="stat-card"><div class="stat-label">Database Status</div><div id="stat-db" class="stat-val">-</div></div>
        <div class="stat-card"><div class="stat-label">Active Teams</div><div id="stat-teams" class="stat-val">-</div></div>
        <div class="stat-card"><div class="stat-label">Total Submissions</div><div id="stat-subs" class="stat-val">-</div></div>
        <div class="stat-card"><div class="stat-label">Hidden Tasks Available</div><div id="stat-hidden" class="stat-val">-</div></div>
        <div class="stat-card"><div class="stat-label">Avg Tool Latency</div><div id="stat-lat" class="stat-val">- ms</div></div>
      </div>
    </div>
  </div>

  <!-- Teams Tab -->
  <div id="tab-teams" class="tab-content">
    <div class="panel">
      <div class="controls">
        <input id="team-search" type="text" placeholder="Search team name..." oninput="loadTeams()">
        <select id="team-status-filter" onchange="loadTeams()">
          <option value="">All Statuses</option>
          <option value="active">Active</option>
          <option value="suspended">Suspended</option>
          <option value="disqualified">Disqualified</option>
        </select>
        <button onclick="showCreateTeamModal()">+ Register Team</button>
      </div>
      <table id="teams-table">
        <thead>
          <tr><th>Team Name</th><th>Team ID</th><th>Status</th><th>Token Ver</th><th>Submissions</th><th>Actions</th></tr>
        </thead>
        <tbody></tbody>
      </table>
    </div>
  </div>

  <!-- Settings Tab -->
  <div id="tab-settings" class="tab-content">
    <div class="panel">
      <h2 style="font-size:16px;margin-bottom:12px;">Live Runtime Settings</h2>
      <table id="settings-table">
        <thead><tr><th>Key</th><th>Value</th><th>Updated By</th><th>Actions</th></tr></thead>
        <tbody></tbody>
      </table>
    </div>
  </div>

  <!-- Leaderboard Tab -->
  <div id="tab-leaderboard" class="tab-content">
    <div class="panel">
      <div class="controls">
        <label style="font-size:13px;color:var(--muted)">View Mode:</label>
        <select id="lead-mode" onchange="loadLeaderboard()">
          <option value="best">Best Submission</option>
          <option value="latest">Latest Submission</option>
        </select>
        <button onclick="exportLeaderboardCSV()">Export CSV</button>
        <button class="btn-secondary" onclick="loadLeaderboard()">Refresh</button>
      </div>
      <table id="leaderboard-table">
        <thead>
          <tr><th>Rank</th><th>Team</th><th>Team ID</th><th>Score</th><th>Duration</th><th>Task Success</th><th>Policy</th><th>Robustness</th><th>Evidence</th><th>Calibration</th><th>Efficiency</th></tr>
        </thead>
        <tbody></tbody>
      </table>
    </div>
  </div>

  <!-- Submissions Tab -->
  <div id="tab-submissions" class="tab-content">
    <div class="panel">
      <h2 style="font-size:16px;margin-bottom:12px;">Submissions Evaluation</h2>
      <table id="submissions-table">
        <thead><tr><th>Submission ID</th><th>Team</th><th>Team ID</th><th>Attempt</th><th>Status</th><th>Score</th><th>Duration</th><th>Tool Calls</th><th>Actions</th></tr></thead>
        <tbody></tbody>
      </table>
    </div>
  </div>

  <!-- Logs Tab -->
  <div id="tab-logs" class="tab-content">
    <div class="panel">
      <div class="controls">
        <label><input id="log-rejections" type="checkbox" onchange="loadLogs()"> Rejections Only</label>
        <button class="btn-secondary" onclick="loadLogs()">Refresh Logs</button>
      </div>
      <table id="logs-table">
        <thead><tr><th>Time</th><th>Team</th><th>Tool</th><th>Rejected?</th><th>Latency</th></tr></thead>
        <tbody></tbody>
      </table>
    </div>
  </div>

  <script>
    let adminSecret = sessionStorage.getItem('adminSecret') || '';

    function checkAuth() {
      if (!adminSecret) {
        document.getElementById('auth-modal').style.display = 'flex';
      } else {
        loadHealth();
      }
    }

    function authenticate() {
      const val = document.getElementById('secret-input').value.trim();
      if (!val) return;
      adminSecret = val;
      sessionStorage.setItem('adminSecret', val);
      document.getElementById('auth-modal').style.display = 'none';
      loadHealth();
    }

    async function api(path, options = {}) {
      options.headers = options.headers || {};
      options.headers['X-Admin-Secret'] = adminSecret;
      if (options.body && typeof options.body === 'object') {
        options.headers['Content-Type'] = 'application/json';
        options.body = JSON.stringify(options.body);
      }
      const res = await fetch('/admin' + path, options);
      if (res.status === 401) {
        sessionStorage.removeItem('adminSecret');
        adminSecret = '';
        checkAuth();
        throw new Error('Unauthorized');
      }
      return res;
    }

    function switchTab(name) {
      document.querySelectorAll('.nav-tab').forEach(t => t.classList.remove('active'));
      document.querySelectorAll('.tab-content').forEach(c => c.classList.remove('active'));
      event.target.classList.add('active');
      document.getElementById('tab-' + name).classList.add('active');
      if (name === 'overview') loadHealth();
      if (name === 'teams') loadTeams();
      if (name === 'settings') loadSettings();
      if (name === 'leaderboard') loadLeaderboard();
      if (name === 'submissions') loadSubmissions();
      if (name === 'logs') loadLogs();
    }

    async function loadHealth() {
      try {
        const res = await api('/health');
        const d = await res.json();
        document.getElementById('system-badge').innerText = 'System: ' + d.status.toUpperCase();
        document.getElementById('phase-badge').innerText = 'Phase: ' + d.competition_phase.toUpperCase();
        document.getElementById('stat-db').innerText = d.database;
        document.getElementById('stat-teams').innerText = d.active_teams_count;
        document.getElementById('stat-subs').innerText = d.total_submissions_count;
        document.getElementById('stat-hidden').innerText = d.hidden_tasks_available;
        document.getElementById('stat-lat').innerText = d.average_tool_latency_ms + ' ms';
      } catch (e) {}
    }

    async function loadTeams() {
      const q = document.getElementById('team-search').value;
      const status = document.getElementById('team-status-filter').value;
      let url = '/teams?limit=50';
      if (q) url += '&q=' + encodeURIComponent(q);
      if (status) url += '&status=' + encodeURIComponent(status);
      const res = await api(url);
      const d = await res.json();
      const tbody = document.querySelector('#teams-table tbody');
      tbody.innerHTML = d.teams.map(t => `
        <tr>
          <td><strong>${t.team_name}</strong><br><span style="font-size:11px;color:var(--muted)">${t.team_id}</span></td>
          <td><span class="badge" style="background:rgba(56,189,248,0.2);color:var(--primary);font-size:12px;font-weight:700;">${t.team_code || t.display_id || '-'}</span></td>
          <td><span class="badge" style="background:${t.status==='active'?'rgba(74,222,128,0.2)':'rgba(248,113,113,0.2)'}">${t.status}</span></td>
          <td>${t.token_version}</td>
          <td>${t.submissions_count}</td>
          <td>
            <button class="btn-secondary" onclick="regenerateToken('${t.team_id}')">Regenerate Token</button>
            ${t.status === 'active'
              ? `<button class="btn-danger" onclick="setTeamStatus('${t.team_id}','suspended')">Suspend</button>`
              : `<button onclick="setTeamStatus('${t.team_id}','active')">Activate</button>`}
          </td>
        </tr>
      `).join('');
    }

    function showCreateTeamModal() {
      document.getElementById('create-team-name').value = '';
      document.getElementById('create-team-members').value = '';
      document.getElementById('create-team-github').value = '';
      document.getElementById('team-modal-error').style.display = 'none';
      document.getElementById('team-modal').style.display = 'flex';
    }

    function closeCreateTeamModal() {
      document.getElementById('team-modal').style.display = 'none';
    }

    async function submitCreateTeam() {
      const name = document.getElementById('create-team-name').value.trim();
      const errEl = document.getElementById('team-modal-error');
      if (!name) {
        errEl.innerText = 'Team Name is required.';
        errEl.style.display = 'block';
        return;
      }
      const rawMembers = document.getElementById('create-team-members').value.trim();
      const members = rawMembers ? rawMembers.split(',').map(m => m.trim()).filter(Boolean) : [];
      const github = document.getElementById('create-team-github').value.trim() || null;

      try {
        const res = await api('/teams', {
          method: 'POST',
          body: {
            team_name: name,
            members: members,
            github_repo_url: github
          }
        });
        if (!res.ok) {
          const errData = await res.json();
          throw new Error(errData.detail || 'Registration failed');
        }
        const d = await res.json();
        closeCreateTeamModal();
        loadTeams();
        showTokenModal(d.env_snippet, d.team_name);
      } catch (e) {
        errEl.innerText = 'Registration failed: ' + (e.message || 'Unknown error');
        errEl.style.display = 'block';
      }
    }

    function showTokenModal(snippet, teamName) {
      document.getElementById('token-modal-title').innerText = teamName ? ('Credentials for ' + teamName) : 'Team Credentials';
      document.getElementById('token-modal-snippet').innerText = snippet;
      document.getElementById('token-modal').style.display = 'flex';
      const btn = document.getElementById('copy-token-btn');
      btn.innerText = '📋 Copy to Clipboard';
    }

    function closeTokenModal() {
      document.getElementById('token-modal').style.display = 'none';
    }

    function copyTokenSnippet() {
      const text = document.getElementById('token-modal-snippet').innerText;
      navigator.clipboard.writeText(text).then(() => {
        const btn = document.getElementById('copy-token-btn');
        btn.innerText = '✅ Copied!';
        setTimeout(() => { btn.innerText = '📋 Copy to Clipboard'; }, 2000);
      }).catch(() => {
        alert('Could not copy automatically. Please copy the text manually.');
      });
    }

    async function setTeamStatus(teamId, status) {
      if (!confirm('Confirm status update to ' + status + '?')) return;
      await api('/teams/' + teamId + '/status', { method: 'POST', body: { status } });
      loadTeams();
    }

    async function regenerateToken(teamId) {
      if (!confirm('Regenerating will immediately revoke the team\\'s active token. Proceed?')) return;
      try {
        const res = await api('/teams/' + teamId + '/regenerate-token', { method: 'POST' });
        const d = await res.json();
        loadTeams();
        showTokenModal(d.env_snippet, d.team_name);
      } catch (e) {
        alert('Token regeneration failed: ' + (e.message || 'Unknown error'));
      }
    }

    async function loadLeaderboard() {
      const mode = document.getElementById('lead-mode').value;
      const res = await api('/leaderboard?mode=' + mode);
      const d = await res.json();
      const tbody = document.querySelector('#leaderboard-table tbody');
      tbody.innerHTML = d.leaderboard.map(e => `
        <tr>
          <td><strong>#${e.rank}</strong></td>
          <td><strong>${e.team_name}</strong></td>
          <td><span class="badge" style="background:rgba(56,189,248,0.2);color:var(--primary);font-size:11px;">${e.team_code || '-'}</span></td>
          <td><strong>${(e.aggregate_score * 100).toFixed(2)}%</strong></td>
          <td>${e.duration_seconds !== null && e.duration_seconds !== undefined ? e.duration_seconds.toFixed(1) + 's' : '-'}</td>
          <td>${(e.task_success * 100).toFixed(1)}%</td>
          <td>${(e.policy * 100).toFixed(1)}%</td>
          <td>${(e.robustness * 100).toFixed(1)}%</td>
          <td>${(e.evidence * 100).toFixed(1)}%</td>
          <td>${(e.calibration * 100).toFixed(1)}%</td>
          <td>${(e.efficiency * 100).toFixed(1)}%</td>
        </tr>
      `).join('');
    }

    function exportLeaderboardCSV() {
      const mode = document.getElementById('lead-mode').value;
      window.open('/admin/leaderboard?mode=' + mode + '&export=csv&secret=' + encodeURIComponent(adminSecret));
    }

    async function loadSettings() {
      const res = await api('/settings');
      const d = await res.json();
      const tbody = document.querySelector('#settings-table tbody');
      tbody.innerHTML = d.settings.map(s => `
        <tr>
          <td><code>${s.key}</code></td>
          <td><pre style="max-height:80px;">${JSON.stringify(s.value, null, 2)}</pre></td>
          <td>${s.updated_by || 'system'}</td>
          <td><button class="btn-secondary" onclick="editSetting('${s.key}')">Edit</button></td>
        </tr>
      `).join('');
    }

    async function editSetting(key) {
      const newVal = prompt('Enter new JSON/string value for ' + key + ':');
      if (!newVal) return;
      let parsed;
      try { parsed = JSON.parse(newVal); } catch { parsed = newVal; }
      await api('/settings/' + key, { method: 'PUT', body: { value: parsed } });
      loadSettings();
    }

    async function loadSubmissions() {
      const res = await api('/submissions?limit=30');
      const d = await res.json();
      const tbody = document.querySelector('#submissions-table tbody');
      tbody.innerHTML = d.submissions.map(s => `
        <tr>
          <td><code>${s.submission_id.substring(0,8)}...</code></td>
          <td><strong>${s.team_name}</strong></td>
          <td><span class="badge" style="background:rgba(56,189,248,0.2);color:var(--primary);font-size:11px;">${s.team_code || '-'}</span></td>
          <td>#${s.attempt_number}</td>
          <td><span class="badge" style="background:${s.status==='completed'?'rgba(74,222,128,0.2)':s.status==='in_progress'?'rgba(56,189,248,0.2)':'rgba(248,113,113,0.2)'}">${s.status}</span></td>
          <td>${s.aggregate_score !== null ? (s.aggregate_score * 100).toFixed(2) + '%' : '-'}</td>
          <td>${s.duration_seconds !== null && s.duration_seconds !== undefined ? s.duration_seconds.toFixed(1) + 's' : '-'}</td>
          <td>${s.tool_calls_count !== null && s.tool_calls_count !== undefined ? s.tool_calls_count : '-'}</td>
          <td>
            <button class="btn-secondary" onclick="showSubmissionDetail('${s.submission_id}')">Inspect</button>
            <button class="btn-secondary" onclick="scoreSub('${s.submission_id}', false)">Score</button>
            <button class="btn-danger" onclick="scoreSub('${s.submission_id}', true)">Force Rescore</button>
          </td>
        </tr>
      `).join('');
    }

    async function showSubmissionDetail(subId) {
      try {
        const res = await api('/submissions/' + subId);
        const d = await res.json();
        document.getElementById('sub-modal-title').innerText = d.team_name + (d.team_code ? ' (' + d.team_code + ')' : '') + ' — Attempt #' + d.attempt_number;
        const brk = d.breakdown || {};
        const dims = brk.dimension_scores || {};
        let dimHtml = '<div style="display:grid;grid-template-columns:repeat(auto-fit, minmax(130px, 1fr));gap:8px;margin:12px 0;">';
        for (const [k, v] of Object.entries(dims)) {
          dimHtml += '<div style="background:#0f172a;padding:8px;border-radius:4px;border:1px solid var(--border);"><div style="font-size:11px;color:var(--muted);text-transform:uppercase;">' + k + '</div><div style="font-size:16px;font-weight:700;color:var(--primary);">' + (v * 100).toFixed(1) + '%</div></div>';
        }
        dimHtml += '</div>';

        let toolChips = '';
        if (d.tool_calls_breakdown && Object.keys(d.tool_calls_breakdown).length > 0) {
          toolChips = '<div style="margin:12px 0;"><strong style="font-size:12px;color:var(--muted);text-transform:uppercase;">Tool Calls Breakdown:</strong><div style="display:flex;flex-wrap:wrap;gap:6px;margin-top:6px;">' +
            Object.entries(d.tool_calls_breakdown).map(([tool, cnt]) => '<span class="badge" style="background:rgba(255,255,255,0.06);border:1px solid var(--border);color:var(--text);font-size:11px;">' + tool + ': <strong>' + cnt + '</strong></span>').join('') +
            '</div></div>';
        }

        const tasksEvaluated = d.tasks_evaluated || [];
        let taskHtml = '<h3 style="font-size:14px;margin-top:14px;margin-bottom:6px;">Evaluated Tasks (' + tasksEvaluated.length + ')</h3>';
        if (tasksEvaluated.length > 0) {
          taskHtml += '<table style="font-size:11px;"><thead><tr><th>Task ID</th><th>Status</th><th>Score</th><th>Evidence</th></tr></thead><tbody>';
          taskHtml += tasksEvaluated.slice(0, 30).map(t => '<tr><td><code>' + t.task_id + '</code></td><td>' + (t.status || 'evaluated') + '</td><td>' + ((t.score !== undefined ? (t.score * 100).toFixed(1) + '%' : '-')) + '</td><td>' + (t.evidence_matched ? '✅ Matched' : '❌ Miss') + '</td></tr>').join('');
          taskHtml += '</tbody></table>';
        } else {
          taskHtml += '<p style="color:var(--muted);font-size:12px;">No task audit records scored yet.</p>';
        }

        document.getElementById('sub-modal-content').innerHTML = `
          <div style="display:flex;gap:14px;align-items:center;margin-bottom:12px;flex-wrap:wrap;font-size:13px;">
            <div><strong>Status:</strong> <span class="badge" style="background:${d.status==='completed'?'rgba(74,222,128,0.2)':d.status==='in_progress'?'rgba(56,189,248,0.2)':'rgba(248,113,113,0.2)'};">${d.status}</span></div>
            <div><strong>Score:</strong> <span style="font-weight:700;color:var(--primary);">${d.aggregate_score !== null ? (d.aggregate_score * 100).toFixed(2) + '%' : 'Pending'}</span></div>
            <div><strong>Duration:</strong> ${d.duration_seconds !== null && d.duration_seconds !== undefined ? d.duration_seconds.toFixed(1) + 's' : '-'}</div>
            <div><strong>Tool Calls:</strong> ${d.tool_calls_count !== null && d.tool_calls_count !== undefined ? d.tool_calls_count : '0'}</div>
          </div>
          ${toolChips}
          ${dimHtml}
          ${taskHtml}
        `;
        document.getElementById('submission-modal').style.display = 'flex';
      } catch (e) {
        alert('Failed to load submission detail: ' + e.message);
      }
    }

    function closeSubmissionModal() {
      document.getElementById('submission-modal').style.display = 'none';
    }

    async function scoreSub(id, force) {
      await api('/submissions/' + id + '/score?force=' + force, { method: 'POST' });
      alert('Scoring completed!');
      loadSubmissions();
    }

    async function loadLogs() {
      const rej = document.getElementById('log-rejections').checked;
      const res = await api('/monitoring/tool-logs?limit=40&rejections_only=' + rej);
      const d = await res.json();
      const tbody = document.querySelector('#logs-table tbody');
      tbody.innerHTML = d.logs.map(l => `
        <tr>
          <td>${new Date(l.created_at).toLocaleTimeString()}</td>
          <td><code>${l.team_id.substring(0,8)}</code></td>
          <td>${l.tool_name}</td>
          <td><span class="badge" style="background:${l.was_enforcement_rejection?'rgba(248,113,113,0.2)':'rgba(74,222,128,0.2)'}">${l.was_enforcement_rejection?'REJECTED':'OK'}</span></td>
          <td>${l.latency_ms} ms</td>
        </tr>
      `).join('');
    }

    checkAuth();
  </script>
</body>
</html>
"""


@router.get("/dashboard", response_class=HTMLResponse)
async def get_admin_dashboard():
    """Serves single-page responsive operational dashboard UI."""
    return HTMLResponse(content=DASHBOARD_HTML)
