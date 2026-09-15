from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class DimensionScores(BaseModel):
    model_config = ConfigDict(extra="forbid")

    task_success: float = Field(..., ge=0.0, le=1.0)
    policy: float = Field(..., ge=0.0, le=1.0)
    robustness: float = Field(..., ge=0.0, le=1.0)
    evidence: float = Field(..., ge=0.0, le=1.0)
    calibration: float = Field(..., ge=0.0, le=1.0)
    efficiency: float = Field(..., ge=0.0, le=1.0)
    communication: float = Field(..., ge=0.0, le=1.0)
    task_aggregate: float = Field(..., ge=0.0, le=1.0)


class TaskAuditDetail(BaseModel):
    model_config = ConfigDict(extra="ignore")

    enforcement_rejections: int = 0
    tool_calls_count: int = 0
    duplicate_calls_count: int = 0
    cited_evidence: list[str] = Field(default_factory=list)
    valid_observed_evidence: list[str] = Field(default_factory=list)
    required_evidence: list[str] = Field(default_factory=list)
    precision: float = 0.0
    recall: float = 0.0
    f1: float = 0.0
    explanation: str | None = None


class TaskScoreResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    task_id: str
    status: str  # "completed" | "timed_out" | "unstarted"
    scores: DimensionScores
    audit: dict[str, Any] = Field(default_factory=dict)


class SubmissionScoreResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    submission_id: str
    team_id: str
    attempt_number: int
    status: str
    aggregate_score: float = Field(..., ge=0.0, le=1.0)
    breakdown: dict[str, Any]
    tasks_scored: list[TaskScoreResult]


class TeamScoreSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    team_id: str
    score_aggregation: str  # "best" | "last" | "average"
    team_score: float = Field(..., ge=0.0, le=1.0)
    submissions_count: int
    submission_scores: list[dict[str, Any]]
