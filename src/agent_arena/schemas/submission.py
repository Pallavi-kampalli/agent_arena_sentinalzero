from typing import Any, Literal, Optional
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


class SubmissionStatusResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["in_progress", "completed", "expired"]
    tasks_completed: int
    tasks_total: int
    time_remaining_seconds: int


class SubmissionFinalizeResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    submission_id: str
    status: Literal["completed"]
