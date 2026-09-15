from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ScoreAggregationEnum(str, Enum):
    BEST = "best"
    LAST = "last"
    AVERAGE = "average"


class CompetitionPhaseEnum(str, Enum):
    REGISTRATION = "registration"
    BUILD = "build"
    FROZEN = "frozen"
    EVALUATING = "evaluating"
    RESULTS_PUBLISHED = "results_published"


PHASE_ORDER = [
    CompetitionPhaseEnum.REGISTRATION,
    CompetitionPhaseEnum.BUILD,
    CompetitionPhaseEnum.FROZEN,
    CompetitionPhaseEnum.EVALUATING,
    CompetitionPhaseEnum.RESULTS_PUBLISHED,
]


class ScoringWeightsSchema(BaseModel):
    task_success: float = Field(default=0.45, ge=0.0, le=1.0)
    policy: float = Field(default=0.15, ge=0.0, le=1.0)
    robustness: float = Field(default=0.15, ge=0.0, le=1.0)
    evidence: float = Field(default=0.10, ge=0.0, le=1.0)
    calibration: float = Field(default=0.05, ge=0.0, le=1.0)
    efficiency: float = Field(default=0.05, ge=0.0, le=1.0)
    communication: float = Field(default=0.05, ge=0.0, le=1.0)

    @model_validator(mode="after")
    def validate_weights_sum(self) -> "ScoringWeightsSchema":
        total = (
            self.task_success
            + self.policy
            + self.robustness
            + self.evidence
            + self.calibration
            + self.efficiency
            + self.communication
        )
        if abs(total - 1.0) > 1e-4:
            raise ValueError(f"Scoring weights must sum to 1.0 (current sum: {total:.4f})")
        return self


class SettingUpdatePayload(BaseModel):
    key: str
    value: Any
    changed_by: str = "admin"


class SettingAuditLogResponse(BaseModel):
    id: int
    key: str
    old_value: Any
    new_value: Any
    changed_by: str
    changed_at: datetime

    model_config = ConfigDict(from_attributes=True)


def validate_phase_transition(current_phase: str | None, new_phase: str) -> None:
    if current_phase is None:
        return
    try:
        curr_enum = CompetitionPhaseEnum(current_phase)
        new_enum = CompetitionPhaseEnum(new_phase)
    except ValueError as e:
        raise ValueError(f"Invalid phase value: {e}") from e

    curr_idx = PHASE_ORDER.index(curr_enum)
    new_idx = PHASE_ORDER.index(new_enum)
    if new_idx < curr_idx:
        raise ValueError(
            f"Invalid phase transition: cannot move backward from '{curr_enum.value}' to '{new_enum.value}'"
        )


def validate_setting_value(key: str, value: Any, current_value: Any = None) -> Any:
    """Validates and coerces setting value according to PRD §2.2 rules."""
    if key == "submission_limit_per_team":
        val = int(value)
        if val < 1:
            raise ValueError("submission_limit_per_team must be >= 1")
        return val

    elif key == "score_aggregation":
        if isinstance(value, ScoreAggregationEnum):
            return value.value
        if value not in {e.value for e in ScoreAggregationEnum}:
            raise ValueError(f"score_aggregation must be one of: {[e.value for e in ScoreAggregationEnum]}")
        return str(value)

    elif key == "scoring_weights":
        if isinstance(value, ScoringWeightsSchema):
            return value.model_dump()
        if isinstance(value, dict):
            validated = ScoringWeightsSchema(**value)
            return validated.model_dump()
        raise ValueError("scoring_weights must be a dictionary with all 7 scoring dimensions")

    elif key in {"hidden_task_count", "dev_task_count"} or key in {
        "tool_call_budget_per_task",
        "time_budget_per_task_seconds",
        "rate_limit_tool_calls_per_min",
    }:
        val = int(value)
        if val < 1:
            raise ValueError(f"{key} must be >= 1")
        return val

    elif key == "competition_phase":
        if isinstance(value, CompetitionPhaseEnum):
            phase_val = value.value
        else:
            phase_val = str(value)
        if phase_val not in {e.value for e in CompetitionPhaseEnum}:
            raise ValueError(f"competition_phase must be one of: {[e.value for e in CompetitionPhaseEnum]}")
        validate_phase_transition(current_value, phase_val)
        return phase_val

    elif key in {"competition_start_at", "competition_end_at"}:
        if value is None:
            return None
        if isinstance(value, datetime):
            return value.isoformat()
        if isinstance(value, str):
            # Parse ISO string
            datetime.fromisoformat(value.replace("Z", "+00:00"))
            return value
        raise ValueError(f"{key} must be an ISO 8601 datetime string or null")

    elif key == "allowed_llm_models":
        if value == "*":
            return "*"
        if isinstance(value, list) and all(isinstance(m, str) for m in value):
            return value
        if isinstance(value, str):
            return value
        raise ValueError("allowed_llm_models must be '*' or a list of string model identifiers")

    elif key == "bearer_token_expiry_hours":
        if value is None:
            return None
        val = int(value)
        if val < 1:
            raise ValueError("bearer_token_expiry_hours must be >= 1 or null")
        return val

    else:
        # Unknown or custom setting
        return value
