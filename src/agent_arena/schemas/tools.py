"""Pydantic schemas for the 10 SupportOps tools (6 read tools, 4 action tools)."""

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

# --- Read Tools ---


class SearchKnowledgeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    query: str = Field(..., min_length=1, max_length=500, description="Keyword search query")
    top_k: int = Field(default=5, ge=1, le=50, description="Max number of documents to return")


class SearchKnowledgeResult(BaseModel):
    id: str
    title: str
    snippet: str
    updated_at: str
    category: str


class SearchKnowledgeResponse(BaseModel):
    results: list[dict[str, Any]]


class GetDocumentRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    document_id: str = Field(
        ..., min_length=1, max_length=100, description="Policy or document identifier (e.g. DOC-1001)"
    )


class GetDocumentResponse(BaseModel):
    document: dict[str, Any]


class GetCustomerRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    customer_id: str = Field(..., min_length=1, max_length=100, description="Customer ID (e.g. CUS-1001)")


class GetCustomerResponse(BaseModel):
    customer: dict[str, Any]


class GetTransactionsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    customer_id: str = Field(..., min_length=1, max_length=100, description="Customer ID")
    start_date: str | None = Field(default=None, max_length=50, description="ISO format start date (inclusive)")
    end_date: str | None = Field(default=None, max_length=50, description="ISO format end date (inclusive)")


class GetTransactionsResponse(BaseModel):
    transactions: list[dict[str, Any]]


class GetSubscriptionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    customer_id: str = Field(..., min_length=1, max_length=100, description="Customer ID")


class GetSubscriptionResponse(BaseModel):
    subscription: dict[str, Any] | None = None


class GetPreviousCasesRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    customer_id: str = Field(..., min_length=1, max_length=100, description="Customer ID")
    limit: int = Field(default=5, ge=1, le=50, description="Max cases to return")


class GetPreviousCasesResponse(BaseModel):
    cases: list[dict[str, Any]]


# --- Action Tools ---


class IssueRefundRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    transaction_id: str = Field(..., min_length=1, max_length=100, description="Transaction ID to refund")
    amount: float = Field(..., gt=0, le=1_000_000, allow_inf_nan=False, description="Refund amount (must be positive)")
    reason: str = Field(..., min_length=1, max_length=2000, description="Agent rationale for refund")


class CancelSubscriptionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    customer_id: str = Field(..., min_length=1, max_length=100, description="Customer ID")
    subscription_id: str = Field(..., min_length=1, max_length=100, description="Subscription ID to cancel")


class EscalateCaseRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    case_id: str = Field(..., min_length=1, max_length=100, description="Case or transaction identifier")
    team: str = Field(
        ..., min_length=1, max_length=100, description="Target escalation team (e.g. billing_specialists)"
    )
    reason: str = Field(..., min_length=1, max_length=2000, description="Evidence-grounded rationale for escalation")


class RequestVerificationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    customer_id: str = Field(..., min_length=1, max_length=100, description="Customer ID to verify")
    verification_type: str = Field(
        default="identity", min_length=1, max_length=50, description="Type of verification challenge"
    )


# --- Success & Business Rejection Responses ---


class RefundSuccessResponse(BaseModel):
    status: str = "refunded"
    transaction: dict[str, Any]


class CancelSubscriptionSuccessResponse(BaseModel):
    status: str = "cancelled"
    subscription: dict[str, Any]


class EscalateCaseSuccessResponse(BaseModel):
    status: str = "escalated"


class RequestVerificationSuccessResponse(BaseModel):
    status: str = "verification_requested"


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
