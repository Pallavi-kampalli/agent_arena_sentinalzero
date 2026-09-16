from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class CaseClassification(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    category: str = Field(..., min_length=1, max_length=100)
    issue: str = Field(..., min_length=1, max_length=100)
    severity: Literal["low", "medium", "high", "critical"]


class Decision(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    resolution: Literal["refund", "deny", "escalate", "request_info"]
    escalation_required: bool


class TaskSubmitRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    task_id: str = Field(..., min_length=1, max_length=100)
    case_classification: CaseClassification
    decision: Decision
    evidence: list[str] = Field(default_factory=list, max_length=100)
    uncertainties: list[str] = Field(default_factory=list, max_length=100)
    customer_response: str = Field(..., min_length=1, max_length=10000)
    confidence: float = Field(..., ge=0.0, le=1.0, allow_inf_nan=False)


class TaskSubmitResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    received: bool = True
    task_id: str


class TaskStartResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    task_id: str
    customer_message: str
    customer_id: str


class SubmissionStartResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    submission_id: str
    attempt_number: int
    tasks_total: int
    tasks: list[TaskStartResponse] = Field(default_factory=list)


class SubmissionStatusResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["in_progress", "completed", "expired", "interrupted"]
    tasks_completed: int
    tasks_total: int
    time_remaining_seconds: int


class SubmissionFinalizeResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    submission_id: str
    status: Literal["completed"]


class BatchTaskSubmitItem(BaseModel):
    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)

    task_id: str = Field(..., min_length=1, max_length=100)
    case_classification: CaseClassification
    decision: Decision
    evidence: list[str] = Field(default_factory=list, max_length=100)
    uncertainties: list[str] = Field(default_factory=list, max_length=100)
    customer_response: str = Field(..., min_length=1, max_length=10000)
    confidence: float = Field(..., ge=0.0, le=1.0, allow_inf_nan=False)
    started_at: str | None = None
    completed_at: str | None = None
    task_started_at: str | None = None
    task_completed_at: str | None = None


class BatchSubmissionSubmitRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    answers: list[BatchTaskSubmitItem]


class BatchSubmissionSubmitResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    submission_id: str
    status: str
    tasks_received: int
    duration_seconds: float
    aggregate_score: float | None = None
    breakdown: dict[str, Any] | None = None
    message: str = "Batch submission received and evaluated successfully."


class SubmissionAbortResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    submission_id: str
    status: Literal["interrupted"] = "interrupted"
    message: str = "Submission aborted and will not be scored or counted."
