from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class AdminTeamCreateRequest(BaseModel):
    team_name: str = Field(..., min_length=1, max_length=100, description="Unique display name of the team.")
    members: list[dict[str, Any]] | list[str] | None = Field(default=None, description="Team member names or emails.")
    github_repo_url: str | None = Field(default=None, description="Optional repository link for defense and records.")

    model_config = ConfigDict(extra="forbid")


class AdminTeamUpdateRequest(BaseModel):
    team_name: str | None = Field(default=None, min_length=1, max_length=100)
    members: list[dict[str, Any]] | list[str] | None = None
    github_repo_url: str | None = None

    model_config = ConfigDict(extra="forbid")


class AdminTeamStatusRequest(BaseModel):
    status: str = Field(..., pattern="^(active|suspended|disqualified)$", description="New status for the team.")

    model_config = ConfigDict(extra="forbid")


class AdminTeamBulkImportRequest(BaseModel):
    csv_content: str | None = Field(
        default=None, description="Raw CSV text with columns: team_name,members,github_repo_url"
    )
    teams: list[AdminTeamCreateRequest] | None = Field(default=None, description="Alternative structured list of teams")

    model_config = ConfigDict(extra="forbid")


class AdminTeamSummary(BaseModel):
    team_id: str
    team_name: str
    display_id: int | None = None
    team_code: str | None = None
    status: str
    token_version: int
    github_repo_url: str | None = None
    members: Any | None = None
    created_at: datetime
    submissions_count: int = 0
    latest_score: float | None = None

    model_config = ConfigDict(from_attributes=True)


class AdminTeamCreateResponse(BaseModel):
    team_id: str
    team_name: str
    display_id: int | None = None
    team_code: str | None = None
    status: str
    token_version: int
    bearer_token: str
    token: str = ""
    env_snippet: str
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


class AdminTokenRegenerateResponse(BaseModel):
    team_id: str
    team_name: str
    token_version: int
    bearer_token: str
    token: str = ""
    env_snippet: str


class AdminTeamListResponse(BaseModel):
    teams: list[AdminTeamSummary]
    total: int
    offset: int
    limit: int


class AdminTeamDetailResponse(BaseModel):
    team_id: str
    team_name: str
    display_id: int | None = None
    team_code: str | None = None
    status: str
    token_version: int
    github_repo_url: str | None = None
    members: Any | None = None
    created_at: datetime
    updated_at: datetime
    submissions: list[dict[str, Any]] = []

    model_config = ConfigDict(from_attributes=True)


class AdminTeamBulkImportResponse(BaseModel):
    created_teams: list[AdminTeamCreateResponse] = []
    teams: list[AdminTeamCreateResponse] = []
    failed_rows: list[dict[str, Any]] = []
    total_imported: int = 0
    total_rows: int = 0
    created_count: int = 0
    skipped_count: int = 0


class AdminSettingUpdateRequest(BaseModel):
    value: Any = Field(..., description="New value for the setting.")

    model_config = ConfigDict(extra="forbid")


class AdminSettingUpdateResponse(BaseModel):
    key: str
    old_value: Any
    new_value: Any
    changed_by: str
    changed_at: datetime


class AdminSettingItem(BaseModel):
    key: str
    value: Any
    updated_by: str | None = None
    updated_at: datetime | None = None


class AdminSettingsResponse(BaseModel):
    settings: list[AdminSettingItem]


class AdminAuditLogItem(BaseModel):
    id: int
    key: str
    old_value: Any
    new_value: Any
    changed_by: str
    changed_at: datetime

    model_config = ConfigDict(from_attributes=True)


class AdminAuditLogListResponse(BaseModel):
    audit_logs: list[AdminAuditLogItem]
    total: int
    offset: int
    limit: int


class AdminPhaseTransitionRequest(BaseModel):
    new_phase: str = Field(..., pattern="^(registration|build|frozen|evaluating|results_published)$")

    model_config = ConfigDict(extra="forbid")


class AdminPhaseResponse(BaseModel):
    current_phase: str
    allowed_next_phases: list[str]
    competition_start_at: str | None = None
    competition_end_at: str | None = None


class AdminLeaderboardEntry(BaseModel):
    rank: int
    team_id: str
    team_name: str
    team_code: str | None = None
    status: str
    aggregate_score: float
    task_success: float
    policy: float
    robustness: float
    evidence: float
    calibration: float
    efficiency: float
    communication: float
    submissions_count: int
    duration_seconds: float | None = None
    tool_calls_total: int | None = None
    last_submission_at: datetime | None = None


class AdminLeaderboardResponse(BaseModel):
    leaderboard: list[AdminLeaderboardEntry]
    mode: str
    tiebreak_order: list[str]


class AdminSubmissionSummary(BaseModel):
    submission_id: str
    team_id: str
    team_name: str
    team_code: str | None = None
    attempt_number: int
    status: str
    aggregate_score: float | None = None
    duration_seconds: float | None = None
    tool_calls_count: int | None = None
    started_at: datetime
    completed_at: datetime | None = None

    model_config = ConfigDict(from_attributes=True)


class AdminSubmissionListResponse(BaseModel):
    submissions: list[AdminSubmissionSummary]
    total: int
    offset: int
    limit: int


class AdminSubmissionDetailResponse(BaseModel):
    submission_id: str
    team_id: str
    team_name: str
    team_code: str | None = None
    attempt_number: int
    status: str
    aggregate_score: float | None = None
    duration_seconds: float | None = None
    tool_calls_count: int | None = None
    tool_calls_breakdown: dict[str, int] | None = None
    breakdown: dict[str, Any] | None = None
    per_task_results: list[dict[str, Any]] | None = None
    started_at: datetime
    completed_at: datetime | None = None


class AdminScoreTriggerResponse(BaseModel):
    submission_id: str
    aggregate_score: float
    breakdown: dict[str, Any]
    score_run_id: str
    rescore_applied: bool


class AdminToolLogSummary(BaseModel):
    id: int
    team_id: str
    task_id: str | None = None
    submission_id: str | None = None
    tool_name: str
    was_enforcement_rejection: bool
    latency_ms: float | int
    created_at: datetime
    request_payload: dict[str, Any] | None = None
    response_payload: dict[str, Any] | None = None

    model_config = ConfigDict(from_attributes=True)


class AdminToolLogListResponse(BaseModel):
    logs: list[AdminToolLogSummary]
    total: int
    offset: int
    limit: int


class AdminHealthResponse(BaseModel):
    status: str
    database: str
    competition_phase: str
    active_teams_count: int
    total_submissions_count: int
    hidden_tasks_available: int
    hidden_tasks_capacity_per_team: int
    task_pool_warning: bool
    average_tool_latency_ms: float
