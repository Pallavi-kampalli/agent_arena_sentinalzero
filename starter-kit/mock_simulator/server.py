import copy
import hashlib
import json
import os
import sqlite3
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Any, Literal

import uvicorn
from fastapi import FastAPI, Header, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field

# Database and configuration
MOCK_DIR = Path(__file__).resolve().parent
DEV_TASKS_PATH = MOCK_DIR / "dev_tasks.json"
DB_PATH = os.getenv("MOCK_DATABASE_PATH", str(MOCK_DIR / "mock_arena.db"))
REVEAL_GROUND_TRUTH = os.getenv("REVEAL_GROUND_TRUTH", "true").lower() in ("1", "true", "yes")

CENT = Decimal("0.01")


# =============================================================================
# GENERATED FROM src/agent_arena/domain/rules.py & models.py
# DO NOT EDIT MANUALLY - Run `python scripts/export_starter_kit.py` to regenerate
# =============================================================================


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


CENT = Decimal("0.01")


def to_decimal(val: Any) -> Decimal:
    """Converts numeric or string value to 2-decimal Decimal using standard half-up rounding."""
    if isinstance(val, Decimal):
        return val.quantize(CENT, rounding=ROUND_HALF_UP)
    return Decimal(str(val if val is not None else 0)).quantize(CENT, rounding=ROUND_HALF_UP)


def parse_iso(dt_str: str) -> datetime:
    """Parses ISO timestamp string to timezone-aware UTC datetime."""
    try:
        dt = datetime.fromisoformat(dt_str.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return dt.astimezone(UTC)
    except Exception:
        return datetime(2026, 1, 1, tzinfo=UTC)


def get_authoritative_policy(world_state: dict[str, Any], category: str) -> dict[str, Any] | None:
    """Returns the latest authoritative policy document for a given category.

    If multiple documents exist (e.g. current vs stale/superseded), selects the one
    with the latest updated_at timestamp.
    """
    policies = world_state.get("policies", [])
    candidates = [p for p in policies if isinstance(p, dict) and p.get("category") == category]
    if not candidates:
        # Check general documents as fallback
        docs = world_state.get("documents", [])
        candidates = [d for d in docs if isinstance(d, dict) and d.get("category") == category]
    if not candidates:
        return None

    # Sort by updated_at descending
    candidates.sort(key=lambda p: parse_iso(p.get("updated_at", "1970-01-01T00:00:00Z")), reverse=True)
    return candidates[0]


def check_refund_eligibility(
    world_state: dict[str, Any],
    transaction_id: str,
    amount: float,
    reason: str,
) -> EligibilityResult:
    """Canonical refund eligibility check per SupportOps_PS_v2.md §5.2.

    Checks:
    1. Transaction existence and validity
    2. Not already refunded or refund amount doesn't exceed original charge
    3. No active chargeback or fraud investigation (DOC-1842 §4)
    4. Within refund policy time window (e.g. 30 days) from authoritative policy
    5. Amount within limits
    """
    transactions = {t.get("id"): t for t in world_state.get("transactions", []) if isinstance(t, dict) and "id" in t}
    tx = transactions.get(transaction_id)
    if not tx:
        return EligibilityResult(
            is_eligible=False,
            error="TRANSACTION_NOT_FOUND",
            reason=f"Transaction '{transaction_id}' does not exist in account records.",
        )

    refund_policy = get_authoritative_policy(world_state, "refund")
    policy_doc_id = refund_policy.get("id", "DOC-1001") if refund_policy else "DOC-1001"

    # 1. Check already refunded or amount exceedance (exact Decimal arithmetic)
    already_refunded = tx.get("refund_status") == "refunded"
    total_refunded = to_decimal(tx.get("refunded_amount", 0.0))
    tx_amount = to_decimal(tx.get("amount", 0.0))
    amount_to_refund = to_decimal(amount)

    if already_refunded or (total_refunded >= tx_amount):
        return EligibilityResult(
            is_eligible=False,
            error="INELIGIBLE",
            reason="already_refunded",
            policy_ref=policy_doc_id,
        )

    if (total_refunded + amount_to_refund) > tx_amount:
        return EligibilityResult(
            is_eligible=False,
            error="INELIGIBLE",
            reason="amount_exceeds_transaction",
            policy_ref=policy_doc_id,
        )

    # 2. Check active chargeback / fraud hold (Working example from SupportOps_PS_v2.md §5.2)
    if tx.get("chargeback_status") == "investigation_active" or tx.get("under_fraud_investigation", False):
        hold_policy = get_authoritative_policy(world_state, "dispute_hold")
        hold_doc_id = hold_policy.get("id", "DOC-1842") if hold_policy else "DOC-1842"
        return EligibilityResult(
            is_eligible=False,
            error="INELIGIBLE",
            reason="chargeback_investigation_active",
            policy_ref=hold_doc_id,
        )

    # 3. Check refund window against authoritative policy
    max_days = refund_policy.get("rules", {}).get("refund_window_days", 30) if refund_policy else 30

    current_date_str = world_state.get("current_date", "2026-09-15T00:00:00Z")
    current_dt = parse_iso(current_date_str)
    tx_dt = parse_iso(tx.get("date", current_date_str))
    days_diff = (current_dt - tx_dt).days

    if days_diff > max_days:
        return EligibilityResult(
            is_eligible=False,
            error="INELIGIBLE",
            reason="outside_refund_window",
            policy_ref=policy_doc_id,
        )

    # All checks pass
    return EligibilityResult(
        is_eligible=True,
        status="refunded",
    )


def check_cancellation_eligibility(
    world_state: dict[str, Any],
    customer_id: str,
    subscription_id: str,
) -> EligibilityResult:
    """Canonical cancellation eligibility check per SupportOps_PS_v2.md §5.3.

    Checks:
    1. Subscription existence and ownership
    2. Active subscription status
    3. Contractual lock-in period (requires approved exception to cancel early)
    4. Unresolved billing dispute blocking cancellation
    """
    subscriptions = {s.get("id"): s for s in world_state.get("subscriptions", []) if isinstance(s, dict) and "id" in s}
    sub = subscriptions.get(subscription_id)
    if not sub:
        return EligibilityResult(
            is_eligible=False,
            error="SUBSCRIPTION_NOT_FOUND",
            reason=f"Subscription '{subscription_id}' does not exist.",
        )

    if sub.get("customer_id") != customer_id:
        return EligibilityResult(
            is_eligible=False,
            error="INELIGIBLE",
            reason="subscription_customer_mismatch",
        )

    cancel_policy = get_authoritative_policy(world_state, "cancellation")
    policy_doc_id = cancel_policy.get("id", "DOC-1003") if cancel_policy else "DOC-1003"

    if sub.get("status") == "cancelled":
        return EligibilityResult(
            is_eligible=False,
            error="INELIGIBLE",
            reason="subscription_already_cancelled",
            policy_ref=policy_doc_id,
        )

    # Check unresolved dispute
    if sub.get("has_unresolved_dispute", False):
        return EligibilityResult(
            is_eligible=False,
            error="INELIGIBLE",
            reason="unresolved_billing_dispute",
            policy_ref=policy_doc_id,
        )

    # Check contractual lock-in
    lock_in_until = sub.get("lock_in_until")
    current_date_str = world_state.get("current_date", "2026-09-15T00:00:00Z")
    if lock_in_until:
        lock_in_dt = parse_iso(lock_in_until)
        current_dt = parse_iso(current_date_str)
        if lock_in_dt > current_dt and not sub.get("has_approved_exception", False):
            return EligibilityResult(
                is_eligible=False,
                error="INELIGIBLE",
                reason="lock_in_period_active",
                policy_ref=policy_doc_id,
            )

    return EligibilityResult(
        is_eligible=True,
        status="cancelled",
    )


def check_escalation_validity(
    world_state: dict[str, Any],
    case_id: str,
    team: str,
    reason: str,
    retrieved_evidence_ids: set[str] | list[str] | None = None,
) -> EligibilityResult:
    """Canonical escalation check per SupportOps_PS_v2.md §4.2, §5.3.

    Must include a reason grounded in something retrievable (a policy ref or evidence ID).
    Empty or generic reasons ('customer mad', 'need help') are rejected.
    Keyword mentions ('fraud', 'chargeback') without citing retrievable evidence are strictly rejected.
    """
    if not reason or len(reason.strip()) < 5:
        return EligibilityResult(
            is_eligible=False,
            error="INVALID_ESCALATION",
            reason="reason_not_grounded",
        )

    # Known retrievable IDs in the world state
    retrievable_ids = set()
    for doc in world_state.get("policies", []):
        if isinstance(doc, dict) and doc.get("id"):
            retrievable_ids.add(doc.get("id"))
    for doc in world_state.get("documents", []):
        if isinstance(doc, dict) and doc.get("id"):
            retrievable_ids.add(doc.get("id"))
    for tx in world_state.get("transactions", []):
        if isinstance(tx, dict) and tx.get("id"):
            retrievable_ids.add(tx.get("id"))
    for cs in world_state.get("historical_cases", []):
        if isinstance(cs, dict) and cs.get("case_id"):
            retrievable_ids.add(cs.get("case_id"))
    for sub in world_state.get("subscriptions", []):
        if isinstance(sub, dict) and sub.get("id"):
            retrievable_ids.add(sub.get("id"))
    for cust in world_state.get("customers", []):
        if isinstance(cust, dict) and cust.get("id"):
            retrievable_ids.add(cust.get("id"))

    # If retrieved_evidence_ids is provided (runtime checking in Phase 2/scoring),
    # verify the reason cites an ID that was actually retrieved by the team.
    candidate_ids = set(retrieved_evidence_ids) if retrieved_evidence_ids is not None else retrievable_ids

    # Reason must cite at least one candidate evidence ID
    has_grounded_ref = any(eid in reason for eid in candidate_ids if eid)

    if not has_grounded_ref:
        return EligibilityResult(
            is_eligible=False,
            error="INVALID_ESCALATION",
            reason="reason_not_grounded",
        )

    return EligibilityResult(
        is_eligible=True,
        status="escalated",
    )


def apply_action_to_world(
    world_state: dict[str, Any],
    action_type: str,
    params: dict[str, Any],
    retrieved_evidence_ids: set[str] | list[str] | None = None,
) -> tuple[dict[str, Any], EligibilityResult]:
    """Applies an action to a working copy of world state.

    Returns (mutated_world_state, eligibility_result).
    If ineligible, world state remains completely unmodified (PS §5.1).
    """
    state_copy = copy.deepcopy(world_state)

    if action_type == "issue_refund":
        tx_id = params.get("transaction_id", "")
        amount_dec = to_decimal(params.get("amount", 0.0))
        reason = params.get("reason", "")
        result = check_refund_eligibility(state_copy, tx_id, float(amount_dec), reason)
        if result.is_eligible:
            for tx in state_copy.get("transactions", []):
                if isinstance(tx, dict) and tx.get("id") == tx_id:
                    current_refunded = to_decimal(tx.get("refunded_amount", 0.0))
                    tx_amt = to_decimal(tx.get("amount", 0.0))
                    new_refunded = current_refunded + amount_dec
                    tx["refunded_amount"] = float(new_refunded)
                    tx["refunded_at"] = state_copy.get("current_date", "2026-09-15T00:00:00Z")
                    if new_refunded >= tx_amt:
                        tx["refund_status"] = "refunded"
                    else:
                        tx["refund_status"] = "partially_refunded"
                    break
        return state_copy if result.is_eligible else world_state, result

    elif action_type == "cancel_subscription":
        cust_id = params.get("customer_id", "")
        sub_id = params.get("subscription_id", "")
        result = check_cancellation_eligibility(state_copy, cust_id, sub_id)
        if result.is_eligible:
            for sub in state_copy.get("subscriptions", []):
                if isinstance(sub, dict) and sub.get("id") == sub_id:
                    sub["status"] = "cancelled"
                    sub["cancelled_at"] = state_copy.get("current_date", "2026-09-15T00:00:00Z")
                    sub["auto_renew"] = False
                    break
        return state_copy if result.is_eligible else world_state, result

    elif action_type == "escalate_case":
        case_id = params.get("case_id", "")
        team = params.get("team", "")
        reason = params.get("reason", "")
        result = check_escalation_validity(state_copy, case_id, team, reason, retrieved_evidence_ids)
        if result.is_eligible:
            escalations = state_copy.setdefault("escalations", [])
            escalations.append(
                {
                    "case_id": case_id,
                    "team": team,
                    "reason": reason,
                    "timestamp": state_copy.get("current_date", "2026-09-15T00:00:00Z"),
                }
            )
        return state_copy if result.is_eligible else world_state, result

    elif action_type == "request_verification":
        # Always succeeds (PS §4.2, §5.3)
        cust_id = params.get("customer_id", "")
        vtype = params.get("verification_type", "identity")
        requests = state_copy.setdefault("verification_requests", [])
        requests.append(
            {
                "customer_id": cust_id,
                "verification_type": vtype,
                "timestamp": state_copy.get("current_date", "2026-09-15T00:00:00Z"),
            }
        )
        return state_copy, EligibilityResult(is_eligible=True, status="verification_requested")

    else:
        return world_state, EligibilityResult(is_eligible=True, status="no_action")


# =============================================================================
# END GENERATED DOMAIN SECTION
# =============================================================================


# =============================================================================
# Pydantic Schemas (Participant-Facing Contract Parity)
# =============================================================================


class SearchKnowledgeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    query: str = Field(..., min_length=1, max_length=500)
    top_k: int = Field(default=5, ge=1, le=20)


class GetDocumentRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    document_id: str = Field(..., min_length=1, max_length=100)


class GetCustomerRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    customer_id: str = Field(..., min_length=1, max_length=100)


class GetTransactionsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    customer_id: str = Field(..., min_length=1, max_length=100)
    start_date: str | None = Field(default=None, max_length=50)
    end_date: str | None = Field(default=None, max_length=50)


class GetSubscriptionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    customer_id: str = Field(..., min_length=1, max_length=100)


class GetPreviousCasesRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    customer_id: str = Field(..., min_length=1, max_length=100)
    limit: int = Field(default=5, ge=1, le=50)


class IssueRefundRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    transaction_id: str = Field(..., min_length=1, max_length=100)
    amount: float = Field(..., gt=0.0, allow_inf_nan=False)
    reason: str = Field(..., min_length=1, max_length=1000)


class CancelSubscriptionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    customer_id: str = Field(..., min_length=1, max_length=100)
    subscription_id: str = Field(..., min_length=1, max_length=100)


class EscalateCaseRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    case_id: str = Field(..., min_length=1, max_length=100)
    team: str = Field(..., min_length=1, max_length=100)
    reason: str = Field(..., min_length=1, max_length=1000)


class RequestVerificationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    customer_id: str = Field(..., min_length=1, max_length=100)
    verification_type: str = Field(default="identity", min_length=1, max_length=100)


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


# Response Schemas
class TaskStartResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    task_id: str
    customer_message: str
    customer_id: str


class TaskSubmitResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    received: bool = True
    task_id: str


class TaskSubmitPracticeResponse(TaskSubmitResponse):
    correct: bool
    expected_resolution: str | None = None
    expected_evidence: list[str] = Field(default_factory=list)
    your_evidence: list[str] = Field(default_factory=list)
    diff_explanation: str


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


# =============================================================================
# SQLite Persistence Layer
# =============================================================================


def init_db(conn: sqlite3.Connection | None = None) -> None:
    """Initializes SQLite tables and loads public dev tasks from dev_tasks.json."""
    close_when_done = False
    if conn is None:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        close_when_done = True
    try:
        with conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS mock_tasks (
                    task_id TEXT PRIMARY KEY,
                    family TEXT NOT NULL,
                    variant TEXT NOT NULL,
                    input_payload TEXT NOT NULL,
                    world_state_seed TEXT NOT NULL,
                    ground_truth_privileged TEXT NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS mock_task_assignments (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL,
                    task_id TEXT NOT NULL REFERENCES mock_tasks(task_id),
                    submission_id TEXT,
                    assigned_at TIMESTAMP NOT NULL,
                    world_runtime_state TEXT NOT NULL
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS mock_tool_call_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL,
                    task_id TEXT NOT NULL,
                    submission_id TEXT,
                    tool_name TEXT NOT NULL,
                    request_payload TEXT NOT NULL,
                    response_payload TEXT NOT NULL,
                    was_enforcement_rejection INTEGER NOT NULL,
                    latency_ms INTEGER NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS mock_submissions (
                    submission_id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    attempt_number INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    started_at TIMESTAMP NOT NULL,
                    completed_at TIMESTAMP,
                    per_task_results TEXT NOT NULL
                )
            """)

            cursor = conn.cursor()
            cursor.execute("SELECT COUNT(*) FROM mock_tasks")
            count = cursor.fetchone()[0]
            if count == 0 and DEV_TASKS_PATH.exists():
                with open(DEV_TASKS_PATH, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    tasks = data.get("tasks", [])
                    for t in tasks:
                        conn.execute(
                            """
                            INSERT OR REPLACE INTO mock_tasks (task_id, family, variant, input_payload, world_state_seed, ground_truth_privileged)
                            VALUES (?, ?, ?, ?, ?, ?)
                            """,
                            (
                                t["task_id"],
                                t["family"],
                                t["variant"],
                                json.dumps(t["input_payload"]),
                                json.dumps(t["world_state_seed"]),
                                json.dumps(t["ground_truth"]),
                            ),
                        )
    finally:
        if close_when_done:
            conn.close()


def get_db_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='mock_tasks'")
    if not cursor.fetchone():
        init_db(conn)
    return conn


def get_session_id(authorization: str | None) -> str:
    """Derives a secure session identifier from the Authorization header using SHA-256."""
    if not authorization:
        return "dev-default-session"
    token = authorization.replace("Bearer ", "").strip()
    return hashlib.sha256(token.encode("utf-8")).hexdigest()[:16]


def get_active_assignment(conn: sqlite3.Connection, session_id: str) -> tuple[int, str, dict[str, Any], str | None]:
    """Retrieves the active task assignment for the session."""
    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT id, task_id, world_runtime_state, submission_id
        FROM mock_task_assignments
        WHERE session_id = ?
        ORDER BY id DESC LIMIT 1
        """,
        (session_id,),
    )
    row = cursor.fetchone()
    if not row:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "error": "NO_ACTIVE_TASK",
                "message": "No active task assignment found for team. Start a task before using tools.",
            },
        )
    return row["id"], row["task_id"], json.loads(row["world_runtime_state"]), row["submission_id"]


def update_world_state(conn: sqlite3.Connection, assignment_id: int, new_state: dict[str, Any]) -> None:
    with conn:
        conn.execute(
            "UPDATE mock_task_assignments SET world_runtime_state = ? WHERE id = ?",
            (json.dumps(new_state), assignment_id),
        )


def log_tool_call(
    conn: sqlite3.Connection,
    session_id: str,
    task_id: str,
    submission_id: str | None,
    tool_name: str,
    request_payload: dict[str, Any],
    response_payload: dict[str, Any],
    was_rejection: bool,
    latency_ms: int,
) -> None:
    with conn:
        conn.execute(
            """
            INSERT INTO mock_tool_call_logs
            (session_id, task_id, submission_id, tool_name, request_payload, response_payload, was_enforcement_rejection, latency_ms)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                session_id,
                task_id,
                submission_id,
                tool_name,
                json.dumps(request_payload),
                json.dumps(response_payload),
                1 if was_rejection else 0,
                latency_ms,
            ),
        )


def get_retrieved_evidence_ids(conn: sqlite3.Connection, session_id: str, task_id: str) -> set[str]:
    cursor = conn.cursor()
    cursor.execute(
        "SELECT response_payload FROM mock_tool_call_logs WHERE session_id = ? AND task_id = ?",
        (session_id, task_id),
    )
    rows = cursor.fetchall()
    retrieved: set[str] = set()
    for row in rows:
        try:
            resp = json.loads(row["response_payload"])
        except Exception:
            continue
        for item in resp.get("results", []):
            if isinstance(item, dict) and "id" in item:
                retrieved.add(item["id"])
        doc = resp.get("document")
        if isinstance(doc, dict) and "id" in doc:
            retrieved.add(doc["id"])
        cust = resp.get("customer")
        if isinstance(cust, dict) and "id" in cust:
            retrieved.add(cust["id"])
        for tx in resp.get("transactions", []):
            if isinstance(tx, dict):
                if "id" in tx:
                    retrieved.add(tx["id"])
                if "invoice_id" in tx:
                    retrieved.add(tx["invoice_id"])
        sub = resp.get("subscription")
        if isinstance(sub, dict) and "id" in sub:
            retrieved.add(sub["id"])
        for c in resp.get("cases", []):
            if isinstance(c, dict):
                if "case_id" in c:
                    retrieved.add(c["case_id"])
                if "id" in c:
                    retrieved.add(c["id"])
                for eid in c.get("evidence_used", []):
                    retrieved.add(eid)
        tx_ref = resp.get("transaction")
        if isinstance(tx_ref, dict) and "id" in tx_ref:
            retrieved.add(tx_ref["id"])
        if resp.get("policy_ref"):
            retrieved.add(resp["policy_ref"])
    return retrieved


# =============================================================================
# Tool Execution Logic (Parity with ToolService)
# =============================================================================


def run_read_tool(tool_name: str, world_state: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
    if tool_name == "search_knowledge":
        query = payload["query"]
        top_k = payload.get("top_k", 5)
        all_docs = []
        seen_ids = set()
        for pol in world_state.get("policies", []):
            if isinstance(pol, dict) and pol.get("id") and pol["id"] not in seen_ids:
                seen_ids.add(pol["id"])
                all_docs.append(pol)
        for doc in world_state.get("documents", []):
            if isinstance(doc, dict) and doc.get("id") and doc["id"] not in seen_ids:
                seen_ids.add(doc["id"])
                all_docs.append(doc)

        query_tokens = [w.lower() for w in query.split() if len(w) > 1]
        scored_docs = []
        for d in all_docs:
            title = d.get("title", "").lower()
            content = d.get("content", "").lower()
            doc_id = d.get("id", "").lower()
            category = d.get("category", "").lower()
            score = 0
            if query.lower() in title or query.lower() in content:
                score += 10
            for token in query_tokens:
                if token in doc_id:
                    score += 8
                if token in title:
                    score += 5
                if token in category:
                    score += 3
                if token in content:
                    score += 1
            if score > 0 or not query_tokens:
                full_content = d.get("content", "")
                snippet = full_content[:300] + ("..." if len(full_content) > 300 else "")
                scored_docs.append(
                    (
                        score,
                        parse_iso(d.get("updated_at", "1970-01-01T00:00:00Z")),
                        d["id"],
                        {
                            "id": d["id"],
                            "title": d.get("title", ""),
                            "snippet": snippet,
                            "updated_at": d.get("updated_at", ""),
                            "category": d.get("category", "general"),
                        },
                    )
                )
        scored_docs.sort(key=lambda x: (-x[0], -x[1].timestamp(), x[2]))
        return {"results": [item[3] for item in scored_docs[:top_k]]}

    elif tool_name == "get_document":
        doc_id = payload["document_id"]
        for pol in world_state.get("policies", []):
            if pol.get("id") == doc_id:
                return {"document": pol}
        for doc in world_state.get("documents", []):
            if doc.get("id") == doc_id:
                return {"document": doc}
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": "DOCUMENT_NOT_FOUND", "message": f"Document '{doc_id}' not found in task knowledge base."},
        )

    elif tool_name == "get_customer":
        cust_id = payload["customer_id"]
        for cust in world_state.get("customers", []):
            if cust.get("id") == cust_id:
                return {"customer": cust}
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": "CUSTOMER_NOT_FOUND", "message": f"Customer '{cust_id}' not found in account records."},
        )

    elif tool_name == "get_transactions":
        cust_id = payload["customer_id"]
        cust_exists = any(c.get("id") == cust_id for c in world_state.get("customers", []))
        if not cust_exists:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={"error": "CUSTOMER_NOT_FOUND", "message": f"Customer '{cust_id}' not found."},
            )
        start_date = payload.get("start_date")
        end_date = payload.get("end_date")
        start_dt = None
        end_dt = None
        if start_date:
            try:
                start_dt = parse_iso(start_date)
            except Exception as e:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                    detail={"error": "INVALID_DATE_FORMAT", "message": f"Invalid start_date '{start_date}': {e}"},
                )
        if end_date:
            try:
                end_dt = parse_iso(end_date)
            except Exception as e:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                    detail={"error": "INVALID_DATE_FORMAT", "message": f"Invalid end_date '{end_date}': {e}"},
                )
        if start_dt and end_dt and start_dt > end_dt:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail={
                    "error": "INVALID_DATE_RANGE",
                    "message": f"start_date '{start_date}' cannot be after end_date '{end_date}'.",
                },
            )
        txs = [t for t in world_state.get("transactions", []) if t.get("customer_id") == cust_id]
        filtered = []
        for t in txs:
            t_dt = parse_iso(t.get("date", "1970-01-01T00:00:00Z"))
            if start_dt and t_dt < start_dt:
                continue
            if end_dt and t_dt > end_dt:
                continue
            filtered.append(t)
        filtered.sort(key=lambda t: parse_iso(t.get("date", "1970-01-01T00:00:00Z")), reverse=True)
        return {"transactions": filtered}

    elif tool_name == "get_subscription":
        cust_id = payload["customer_id"]
        cust_exists = any(c.get("id") == cust_id for c in world_state.get("customers", []))
        if not cust_exists:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={"error": "CUSTOMER_NOT_FOUND", "message": f"Customer '{cust_id}' not found."},
            )
        for sub in world_state.get("subscriptions", []):
            if sub.get("customer_id") == cust_id:
                return {"subscription": sub}
        return {"subscription": None}

    elif tool_name == "get_previous_cases":
        cust_id = payload["customer_id"]
        limit = payload.get("limit", 5)
        cust_exists = any(c.get("id") == cust_id for c in world_state.get("customers", []))
        if not cust_exists:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={"error": "CUSTOMER_NOT_FOUND", "message": f"Customer '{cust_id}' not found."},
            )
        cases = [c for c in world_state.get("historical_cases", []) if c.get("customer_id") == cust_id]
        cases.sort(key=lambda c: parse_iso(c.get("date", "1970-01-01T00:00:00Z")), reverse=True)
        return {"cases": cases[:limit]}

    raise HTTPException(status_code=400, detail={"error": "UNKNOWN_TOOL", "message": f"Unknown tool '{tool_name}'"})


def run_action_tool(
    tool_name: str,
    world_state: dict[str, Any],
    payload: dict[str, Any],
    retrieved_evidence_ids: set[str],
) -> tuple[dict[str, Any] | None, dict[str, Any], bool]:
    """Executes state-changing action tools with server-side enforcement.

    Returns (mutated_state_or_None, response_dict, was_rejection).
    """
    if tool_name == "issue_refund":
        tx_id = payload["transaction_id"]
        amount = payload["amount"]
        reason = payload["reason"]
        result = check_refund_eligibility(world_state, tx_id, amount, reason)
        if result.error == "TRANSACTION_NOT_FOUND":
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={
                    "error": "TRANSACTION_NOT_FOUND",
                    "message": f"Transaction '{tx_id}' does not exist in account records.",
                },
            )
        if not result.is_eligible:
            return (
                None,
                {
                    "error": "INELIGIBLE",
                    "reason": result.reason or "ineligible_action",
                    "policy_ref": result.policy_ref,
                },
                True,
            )

        mutated, _ = apply_action_to_world(world_state, "issue_refund", payload)
        updated_tx = next(t for t in mutated["transactions"] if t.get("id") == tx_id)
        return mutated, {"status": updated_tx.get("refund_status", "refunded"), "transaction": updated_tx}, False

    elif tool_name == "cancel_subscription":
        cust_id = payload["customer_id"]
        sub_id = payload["subscription_id"]
        cust_exists = any(c.get("id") == cust_id for c in world_state.get("customers", []) if isinstance(c, dict))
        if not cust_exists:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={"error": "CUSTOMER_NOT_FOUND", "message": f"Customer '{cust_id}' not found."},
            )
        result = check_cancellation_eligibility(world_state, cust_id, sub_id)
        if result.error == "SUBSCRIPTION_NOT_FOUND":
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={"error": "SUBSCRIPTION_NOT_FOUND", "message": f"Subscription '{sub_id}' does not exist."},
            )
        if not result.is_eligible:
            return (
                None,
                {
                    "error": "INELIGIBLE",
                    "reason": result.reason or "ineligible_action",
                    "policy_ref": result.policy_ref,
                },
                True,
            )

        mutated, _ = apply_action_to_world(world_state, "cancel_subscription", payload)
        updated_sub = next(s for s in mutated["subscriptions"] if s["id"] == sub_id)
        return mutated, {"status": "cancelled", "subscription": updated_sub}, False

    elif tool_name == "escalate_case":
        case_id = payload["case_id"]
        team = payload["team"]
        reason = payload["reason"]
        result = check_escalation_validity(
            world_state=world_state,
            case_id=case_id,
            team=team,
            reason=reason,
            retrieved_evidence_ids=list(retrieved_evidence_ids),
        )
        if not result.is_eligible:
            return None, {"error": "INVALID_ESCALATION", "reason": result.reason or "reason_not_grounded"}, True

        mutated, _ = apply_action_to_world(
            world_state,
            "escalate_case",
            payload,
            retrieved_evidence_ids=list(retrieved_evidence_ids),
        )
        return mutated, {"status": "escalated"}, False

    elif tool_name == "request_verification":
        cust_id = payload["customer_id"]
        cust_exists = any(c.get("id") == cust_id for c in world_state.get("customers", []))
        if not cust_exists:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={"error": "CUSTOMER_NOT_FOUND", "message": f"Customer '{cust_id}' not found."},
            )
        mutated, _ = apply_action_to_world(world_state, "request_verification", payload)
        return mutated, {"status": "verification_requested"}, False

    raise HTTPException(status_code=400, detail={"error": "UNKNOWN_TOOL", "message": f"Unknown tool '{tool_name}'"})


# =============================================================================
# FastAPI Application & Lifecycle
# =============================================================================


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield


app = FastAPI(
    title="Agent Arena SupportOps Mock Simulator",
    version="1.0.0",
    lifespan=lifespan,
)


@app.get("/health")
def health() -> dict[str, Any]:
    return {"status": "ok", "mode": "mock", "reveal_ground_truth": REVEAL_GROUND_TRUTH}


@app.post("/dev/reset")
def dev_reset(authorization: str | None = Header(default=None)) -> dict[str, Any]:
    """Simulator-only endpoint to reset active assignments and tool logs for the caller session."""
    session_id = get_session_id(authorization)
    conn = get_db_connection()
    try:
        with conn:
            conn.execute("DELETE FROM mock_task_assignments WHERE session_id = ?", (session_id,))
            conn.execute("DELETE FROM mock_tool_call_logs WHERE session_id = ?", (session_id,))
            conn.execute("DELETE FROM mock_submissions WHERE session_id = ?", (session_id,))
            cursor = conn.cursor()
            cursor.execute("SELECT COUNT(*) FROM mock_tasks")
            count = cursor.fetchone()[0]
        return {"reset": True, "tasks_available": count}
    finally:
        conn.close()


# --- Tool Endpoints ---


@app.post("/tools/search_knowledge")
def search_knowledge(req: SearchKnowledgeRequest, authorization: str | None = Header(default=None)) -> dict[str, Any]:
    t0 = time.perf_counter()
    session_id = get_session_id(authorization)
    conn = get_db_connection()
    try:
        _, task_id, world_state, submission_id = get_active_assignment(conn, session_id)
        resp = run_read_tool("search_knowledge", world_state, req.model_dump())
        latency_ms = max(1, int((time.perf_counter() - t0) * 1000))
        log_tool_call(
            conn, session_id, task_id, submission_id, "search_knowledge", req.model_dump(), resp, False, latency_ms
        )
        return resp
    finally:
        conn.close()


@app.post("/tools/get_document")
def get_document(req: GetDocumentRequest, authorization: str | None = Header(default=None)) -> dict[str, Any]:
    t0 = time.perf_counter()
    session_id = get_session_id(authorization)
    conn = get_db_connection()
    try:
        _, task_id, world_state, submission_id = get_active_assignment(conn, session_id)
        resp = run_read_tool("get_document", world_state, req.model_dump())
        latency_ms = max(1, int((time.perf_counter() - t0) * 1000))
        log_tool_call(
            conn, session_id, task_id, submission_id, "get_document", req.model_dump(), resp, False, latency_ms
        )
        return resp
    finally:
        conn.close()


@app.post("/tools/get_customer")
def get_customer(req: GetCustomerRequest, authorization: str | None = Header(default=None)) -> dict[str, Any]:
    t0 = time.perf_counter()
    session_id = get_session_id(authorization)
    conn = get_db_connection()
    try:
        _, task_id, world_state, submission_id = get_active_assignment(conn, session_id)
        resp = run_read_tool("get_customer", world_state, req.model_dump())
        latency_ms = max(1, int((time.perf_counter() - t0) * 1000))
        log_tool_call(
            conn, session_id, task_id, submission_id, "get_customer", req.model_dump(), resp, False, latency_ms
        )
        return resp
    finally:
        conn.close()


@app.post("/tools/get_transactions")
def get_transactions(req: GetTransactionsRequest, authorization: str | None = Header(default=None)) -> dict[str, Any]:
    t0 = time.perf_counter()
    session_id = get_session_id(authorization)
    conn = get_db_connection()
    try:
        _, task_id, world_state, submission_id = get_active_assignment(conn, session_id)
        resp = run_read_tool("get_transactions", world_state, req.model_dump())
        latency_ms = max(1, int((time.perf_counter() - t0) * 1000))
        log_tool_call(
            conn, session_id, task_id, submission_id, "get_transactions", req.model_dump(), resp, False, latency_ms
        )
        return resp
    finally:
        conn.close()


@app.post("/tools/get_subscription")
def get_subscription(req: GetSubscriptionRequest, authorization: str | None = Header(default=None)) -> dict[str, Any]:
    t0 = time.perf_counter()
    session_id = get_session_id(authorization)
    conn = get_db_connection()
    try:
        _, task_id, world_state, submission_id = get_active_assignment(conn, session_id)
        resp = run_read_tool("get_subscription", world_state, req.model_dump())
        latency_ms = max(1, int((time.perf_counter() - t0) * 1000))
        log_tool_call(
            conn, session_id, task_id, submission_id, "get_subscription", req.model_dump(), resp, False, latency_ms
        )
        return resp
    finally:
        conn.close()


@app.post("/tools/get_previous_cases")
def get_previous_cases(
    req: GetPreviousCasesRequest, authorization: str | None = Header(default=None)
) -> dict[str, Any]:
    t0 = time.perf_counter()
    session_id = get_session_id(authorization)
    conn = get_db_connection()
    try:
        _, task_id, world_state, submission_id = get_active_assignment(conn, session_id)
        resp = run_read_tool("get_previous_cases", world_state, req.model_dump())
        latency_ms = max(1, int((time.perf_counter() - t0) * 1000))
        log_tool_call(
            conn, session_id, task_id, submission_id, "get_previous_cases", req.model_dump(), resp, False, latency_ms
        )
        return resp
    finally:
        conn.close()


@app.post("/tools/issue_refund")
def issue_refund(req: IssueRefundRequest, authorization: str | None = Header(default=None)) -> dict[str, Any]:
    t0 = time.perf_counter()
    session_id = get_session_id(authorization)
    conn = get_db_connection()
    try:
        assignment_id, task_id, world_state, submission_id = get_active_assignment(conn, session_id)
        retrieved_ids = get_retrieved_evidence_ids(conn, session_id, task_id)
        mutated, resp, was_rejection = run_action_tool("issue_refund", world_state, req.model_dump(), retrieved_ids)
        if mutated is not None and not was_rejection:
            update_world_state(conn, assignment_id, mutated)
        latency_ms = max(1, int((time.perf_counter() - t0) * 1000))
        log_tool_call(
            conn, session_id, task_id, submission_id, "issue_refund", req.model_dump(), resp, was_rejection, latency_ms
        )
        return resp
    finally:
        conn.close()


@app.post("/tools/cancel_subscription")
def cancel_subscription(
    req: CancelSubscriptionRequest, authorization: str | None = Header(default=None)
) -> dict[str, Any]:
    t0 = time.perf_counter()
    session_id = get_session_id(authorization)
    conn = get_db_connection()
    try:
        assignment_id, task_id, world_state, submission_id = get_active_assignment(conn, session_id)
        retrieved_ids = get_retrieved_evidence_ids(conn, session_id, task_id)
        mutated, resp, was_rejection = run_action_tool(
            "cancel_subscription", world_state, req.model_dump(), retrieved_ids
        )
        if mutated is not None and not was_rejection:
            update_world_state(conn, assignment_id, mutated)
        latency_ms = max(1, int((time.perf_counter() - t0) * 1000))
        log_tool_call(
            conn,
            session_id,
            task_id,
            submission_id,
            "cancel_subscription",
            req.model_dump(),
            resp,
            was_rejection,
            latency_ms,
        )
        return resp
    finally:
        conn.close()


@app.post("/tools/escalate_case")
def escalate_case(req: EscalateCaseRequest, authorization: str | None = Header(default=None)) -> dict[str, Any]:
    t0 = time.perf_counter()
    session_id = get_session_id(authorization)
    conn = get_db_connection()
    try:
        assignment_id, task_id, world_state, submission_id = get_active_assignment(conn, session_id)
        retrieved_ids = get_retrieved_evidence_ids(conn, session_id, task_id)
        mutated, resp, was_rejection = run_action_tool("escalate_case", world_state, req.model_dump(), retrieved_ids)
        if mutated is not None and not was_rejection:
            update_world_state(conn, assignment_id, mutated)
        latency_ms = max(1, int((time.perf_counter() - t0) * 1000))
        log_tool_call(
            conn, session_id, task_id, submission_id, "escalate_case", req.model_dump(), resp, was_rejection, latency_ms
        )
        return resp
    finally:
        conn.close()


@app.post("/tools/request_verification")
def request_verification(
    req: RequestVerificationRequest, authorization: str | None = Header(default=None)
) -> dict[str, Any]:
    t0 = time.perf_counter()
    session_id = get_session_id(authorization)
    conn = get_db_connection()
    try:
        assignment_id, task_id, world_state, submission_id = get_active_assignment(conn, session_id)
        retrieved_ids = get_retrieved_evidence_ids(conn, session_id, task_id)
        mutated, resp, was_rejection = run_action_tool(
            "request_verification", world_state, req.model_dump(), retrieved_ids
        )
        if mutated is not None and not was_rejection:
            update_world_state(conn, assignment_id, mutated)
        latency_ms = max(1, int((time.perf_counter() - t0) * 1000))
        log_tool_call(
            conn,
            session_id,
            task_id,
            submission_id,
            "request_verification",
            req.model_dump(),
            resp,
            was_rejection,
            latency_ms,
        )
        return resp
    finally:
        conn.close()


# --- Task Flow Endpoints ---


@app.post("/task/start", response_model=TaskStartResponse)
def start_task(authorization: str | None = Header(default=None)) -> TaskStartResponse:
    session_id = get_session_id(authorization)
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        # Find next task not yet assigned to this session
        cursor.execute(
            """
            SELECT t.task_id, t.input_payload, t.world_state_seed
            FROM mock_tasks t
            WHERE t.task_id NOT IN (
                SELECT task_id FROM mock_task_assignments WHERE session_id = ?
            )
            ORDER BY t.task_id ASC LIMIT 1
            """,
            (session_id,),
        )
        row = cursor.fetchone()
        if not row:
            # If all assigned, pick the earliest or reset loop
            cursor.execute(
                "SELECT task_id, input_payload, world_state_seed FROM mock_tasks ORDER BY task_id ASC LIMIT 1"
            )
            row = cursor.fetchone()

        if not row:
            raise HTTPException(
                status_code=500, detail={"error": "NO_TASKS", "message": "No tasks available in simulator."}
            )

        task_id = row["task_id"]
        input_payload = json.loads(row["input_payload"])
        world_seed = json.loads(row["world_state_seed"])

        # Create fresh working copy
        now_iso = datetime.now(UTC).isoformat()
        with conn:
            conn.execute(
                """
                INSERT INTO mock_task_assignments (session_id, task_id, submission_id, assigned_at, world_runtime_state)
                VALUES (?, ?, ?, ?, ?)
                """,
                (session_id, task_id, None, now_iso, json.dumps(world_seed)),
            )

        return TaskStartResponse(
            task_id=task_id,
            customer_message=input_payload.get("customer_message", ""),
            customer_id=input_payload.get("customer_id", ""),
        )
    finally:
        conn.close()


@app.post(
    "/task/submit", response_model=TaskSubmitResponse | TaskSubmitPracticeResponse, response_model_exclude_none=True
)
def submit_task(
    req: TaskSubmitRequest, authorization: str | None = Header(default=None)
) -> TaskSubmitResponse | TaskSubmitPracticeResponse:
    session_id = get_session_id(authorization)
    conn = get_db_connection()
    try:
        assignment_id, active_task_id, runtime_state, submission_id = get_active_assignment(conn, session_id)
        if active_task_id != req.task_id:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "error": "TASK_ID_MISMATCH",
                    "message": f"Active task is '{active_task_id}', cannot submit '{req.task_id}'.",
                },
            )

        # Production response: strictly received and task_id
        if not REVEAL_GROUND_TRUTH:
            return TaskSubmitResponse(received=True, task_id=req.task_id)

        # Mock reveal practice mode: fetch privileged ground truth
        cursor = conn.cursor()
        cursor.execute("SELECT ground_truth_privileged FROM mock_tasks WHERE task_id = ?", (req.task_id,))
        row = cursor.fetchone()
        if not row:
            return TaskSubmitResponse(received=True, task_id=req.task_id)

        gt = json.loads(row["ground_truth_privileged"])
        exp_res = gt.get("expected_resolution")
        must_esc = gt.get("must_escalate", False)
        req_ev = gt.get("required_evidence", [])

        # Evaluation checks
        resolution_correct = req.decision.resolution == exp_res
        escalation_correct = req.decision.escalation_required == must_esc
        evidence_set = set(req.evidence)
        missing_evidence = [e for e in req_ev if e not in evidence_set]

        correct = resolution_correct and escalation_correct and len(missing_evidence) == 0

        diff_parts = []
        if not resolution_correct:
            diff_parts.append(f"Resolution mismatch: expected '{exp_res}', got '{req.decision.resolution}'.")
        if not escalation_correct:
            diff_parts.append(f"Escalation mismatch: expected {must_esc}, got {req.decision.escalation_required}.")
        if missing_evidence:
            diff_parts.append(f"Missing required evidence IDs: {missing_evidence}.")
        if correct:
            diff_parts.append("Decision, escalation flag, and required evidence match ground truth.")

        return TaskSubmitPracticeResponse(
            received=True,
            task_id=req.task_id,
            correct=correct,
            expected_resolution=exp_res,
            expected_evidence=req_ev,
            your_evidence=req.evidence,
            diff_explanation=" ".join(diff_parts),
        )
    finally:
        conn.close()


# --- Submission Lifecycle Endpoints ---


@app.post("/submission/start", response_model=SubmissionStartResponse)
def start_submission(authorization: str | None = Header(default=None)) -> SubmissionStartResponse:
    session_id = get_session_id(authorization)
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM mock_tasks")
        total_tasks = cursor.fetchone()[0]
        cursor.execute("SELECT COUNT(*) FROM mock_submissions WHERE session_id = ?", (session_id,))
        sub_count = cursor.fetchone()[0]

        sub_id = hashlib.sha256(f"{session_id}-{sub_count + 1}-{time.time()}".encode("utf-8")).hexdigest()[:32]
        now_iso = datetime.now(UTC).isoformat()
        with conn:
            conn.execute(
                """
                INSERT INTO mock_submissions (submission_id, session_id, attempt_number, status, started_at, per_task_results)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (sub_id, session_id, sub_count + 1, "in_progress", now_iso, json.dumps([])),
            )
        return SubmissionStartResponse(submission_id=sub_id, attempt_number=sub_count + 1, tasks_total=total_tasks)
    finally:
        conn.close()


@app.get("/submission/{submission_id}/status", response_model=SubmissionStatusResponse)
def get_submission_status(
    submission_id: str, authorization: str | None = Header(default=None)
) -> SubmissionStatusResponse:
    session_id = get_session_id(authorization)
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT status, per_task_results FROM mock_submissions WHERE submission_id = ? AND session_id = ?",
            (submission_id, session_id),
        )
        row = cursor.fetchone()
        if not row:
            raise HTTPException(
                status_code=404,
                detail={"error": "SUBMISSION_NOT_FOUND", "message": f"Submission '{submission_id}' not found."},
            )
        cursor.execute("SELECT COUNT(*) FROM mock_tasks")
        total_tasks = cursor.fetchone()[0]
        results = json.loads(row["per_task_results"])
        return SubmissionStatusResponse(
            status=row["status"],
            tasks_completed=len(results),
            tasks_total=total_tasks,
            time_remaining_seconds=1800,
        )
    finally:
        conn.close()


@app.post("/submission/{submission_id}/finalize", response_model=SubmissionFinalizeResponse)
def finalize_submission(
    submission_id: str, authorization: str | None = Header(default=None)
) -> SubmissionFinalizeResponse:
    session_id = get_session_id(authorization)
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT status FROM mock_submissions WHERE submission_id = ? AND session_id = ?",
            (submission_id, session_id),
        )
        row = cursor.fetchone()
        if not row:
            raise HTTPException(
                status_code=404,
                detail={"error": "SUBMISSION_NOT_FOUND", "message": f"Submission '{submission_id}' not found."},
            )
        now_iso = datetime.now(UTC).isoformat()
        with conn:
            conn.execute(
                "UPDATE mock_submissions SET status = 'completed', completed_at = ? WHERE submission_id = ?",
                (now_iso, submission_id),
            )
        return SubmissionFinalizeResponse(submission_id=submission_id, status="completed")
    finally:
        conn.close()


if __name__ == "__main__":
    port = int(os.getenv("PORT", "8000"))
    host = os.getenv("HOST", "127.0.0.1")
    print(f"Starting Agent Arena Mock Simulator on http://{host}:{port} (REVEAL_GROUND_TRUTH={REVEAL_GROUND_TRUTH})")
    uvicorn.run(app, host=host, port=port)
