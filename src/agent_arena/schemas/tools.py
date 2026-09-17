"""Pydantic schemas for the 9 SentinelZero cybersecurity triage tools (5 read tools, 4 action tools)."""

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

# =============================================================================
# Read Tools (5 Endpoints)
# =============================================================================


class LookupDirectoryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    identifier: str = Field(
        ...,
        min_length=1,
        max_length=200,
        description="Employee email address or employee ID (e.g. EMP-1001 or alex.smith@sentinel-acme.edu)",
    )


class LookupDirectoryResponse(BaseModel):
    found: bool
    employee: dict[str, Any] | None = None


class GetApprovedDomainsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class GetApprovedDomainsResponse(BaseModel):
    official_domains: list[str] = Field(default_factory=list)
    partner_domains: list[str] = Field(default_factory=list)


class GetEmailHeadersRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    message_id: str = Field(
        ...,
        min_length=1,
        max_length=100,
        description="Message identifier (e.g. MSG-DEV-001)",
    )


class GetEmailHeadersResponse(BaseModel):
    message_id: str
    from_header: str
    reply_to: str
    return_path: str
    originating_ip: str
    originating_domain: str
    auth_results: dict[str, str] = Field(default_factory=dict)


class InspectDomainReputationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    domain: str = Field(
        ...,
        min_length=1,
        max_length=253,
        description="Domain name or URL host to inspect (e.g. sentinel-acme-support.com)",
    )


class InspectDomainReputationResponse(BaseModel):
    domain: str
    domain_id: str | None = None
    is_registered_internal: bool = False
    domain_age_days: int = 0
    reputation: str = "unknown"
    lookalike_of: str | None = None
    threat_score: int = Field(default=0, ge=0, le=100)
    known_tags: list[str] = Field(default_factory=list)


class GetThreadHistoryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    thread_id: str = Field(
        ...,
        min_length=1,
        max_length=100,
        description="Conversation thread identifier (e.g. THR-DEV-006)",
    )


class GetThreadHistoryResponse(BaseModel):
    thread_id: str
    message_count: int
    messages: list[dict[str, Any]] = Field(default_factory=list)


# =============================================================================
# Action Tools (4 Endpoints, Server-Side Enforced)
# =============================================================================


class AllowAndDeliverRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    message_id: str = Field(..., min_length=1, max_length=100, description="Message ID to deliver")
    reason: str = Field(..., min_length=1, max_length=2000, description="Security rationale for allowing message")


class AllowAndDeliverResponse(BaseModel):
    status: str = "delivered"
    message_id: str


class ApplyWarningBannerRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    message_id: str = Field(..., min_length=1, max_length=100, description="Message ID to flag")
    banner_type: str = Field(
        default="EXTERNAL_SENDER",
        min_length=1,
        max_length=100,
        description="Warning banner classification (e.g. EXTERNAL_SENDER, UNVERIFIED_IDENTITY)",
    )
    reason: str = Field(..., min_length=1, max_length=2000, description="Rationale for applying warning banner")


class ApplyWarningBannerResponse(BaseModel):
    status: str = "warning_applied"
    message_id: str
    banner: str


class QuarantineMessageRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    message_id: str = Field(..., min_length=1, max_length=100, description="Message ID to quarantine")
    reason: str = Field(..., min_length=1, max_length=2000, description="Rationale for quarantining message")


class QuarantineMessageResponse(BaseModel):
    status: str = "quarantined"
    message_id: str


class EscalateToTier2SocRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    message_id: str = Field(..., min_length=1, max_length=100, description="Message ID or incident identifier")
    reason: str = Field(
        ...,
        min_length=1,
        max_length=2000,
        description="Evidence-grounded rationale citing at least one retrieved evidence ID",
    )


class EscalateToTier2SocResponse(BaseModel):
    status: str = "escalated_to_soc"
    message_id: str


# =============================================================================
# Success & Business Rejection Responses
# =============================================================================


class IneligibleResponse(BaseModel):
    error: str = "INELIGIBLE"
    reason: str
    policy_ref: str | None = None


class InvalidEscalationResponse(BaseModel):
    error: str = "INVALID_ESCALATION"
    reason: str


class ErrorResponse(BaseModel):
    error: str
    message: str
