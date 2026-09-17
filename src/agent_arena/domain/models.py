from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class ResolutionType(str, Enum):
    ALLOW = "allow"
    WARN = "warn"
    QUARANTINE = "quarantine"
    ESCALATE = "escalate"


class ActionType(str, Enum):
    ALLOW_AND_DELIVER = "allow_and_deliver"
    APPLY_WARNING_BANNER = "apply_warning_banner"
    QUARANTINE_MESSAGE = "quarantine_message"
    ESCALATE_TO_TIER2_SOC = "escalate_to_tier2_soc"
    NONE = "none"


@dataclass
class EligibilityResult:
    is_eligible: bool
    status: str = "success"
    reason: str | None = None
    policy_ref: str | None = None
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        if self.is_eligible:
            return {"status": self.status}
        res = {"error": self.error or "INELIGIBLE"}
        if self.reason:
            res["reason"] = self.reason
        if self.policy_ref:
            res["policy_ref"] = self.policy_ref
        return res


@dataclass
class GroundTruth:
    expected_resolution: ResolutionType
    must_escalate: bool
    required_evidence: list[str]
    expected_action: dict[str, Any]
    expected_end_state: dict[str, Any]
    classification: dict[str, str] = field(
        default_factory=lambda: {
            "category": "cybersecurity_triage",
            "issue": "triage",
            "severity": "medium",
        }
    )

    def to_dict(self) -> dict[str, Any]:
        return {
            "expected_resolution": (
                self.expected_resolution.value
                if isinstance(self.expected_resolution, Enum)
                else str(self.expected_resolution)
            ),
            "must_escalate": self.must_escalate,
            "required_evidence": sorted(list(set(self.required_evidence))),
            "expected_action": self.expected_action,
            "expected_end_state": self.expected_end_state,
            "classification": self.classification,
        }
