import copy
import time
import uuid
from datetime import UTC, datetime
from typing import Any

import sqlalchemy as sa
from fastapi import HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import flag_modified

from agent_arena.domain.rules import (
    apply_action_to_world,
    check_cancellation_eligibility,
    check_escalation_validity,
    check_refund_eligibility,
    parse_iso,
)
from agent_arena.models.submission import Submission
from agent_arena.models.task import Task
from agent_arena.models.task_assignment import TaskAssignment
from agent_arena.models.team import Team
from agent_arena.models.tool_call_log import ToolCallLog
from agent_arena.schemas.tools import (
    CancelSubscriptionSuccessResponse,
    EscalateCaseSuccessResponse,
    IneligibleResponse,
    InvalidEscalationResponse,
    RefundSuccessResponse,
    RequestVerificationSuccessResponse,
)
from agent_arena.services.locks import get_team_lock
from agent_arena.services.rate_limiter import ToolLimiter
from agent_arena.services.settings_service import SettingsService


class ToolService:
    """Core tool execution runtime for all 10 SupportOps tools.

    Guarantees:
    - Team and task runtime isolation (all calls resolve against active TaskAssignment)
    - Monotonic hierarchical row-level locking: Level 1 (Team) -> Level 2 (Submission) -> Level 3 (TaskAssignment)
    - Canonical rule enforcement (zero duplicate eligibility logic)
    - State immutability on enforcement rejection
    - Comprehensive tool call logging with latency tracking and token scrubbing
    """

    def __init__(self, session: AsyncSession, settings_service: SettingsService):
        self.session = session
        self.settings_service = settings_service

    async def get_active_assignment(
        self,
        team_id: uuid.UUID,
        task_id: str | None = None,
        for_update: bool = False,
    ) -> TaskAssignment:
        """Resolves the currently active TaskAssignment for an authenticated team.

        Acquires locks monotonically: Level 2 (Submission) -> Level 3 (TaskAssignment).
        """
        sub = None
        # 1. Level 2: Active Submission row
        sub_stmt = sa.select(Submission).where(Submission.team_id == team_id, Submission.status == "in_progress")
        if for_update:
            sub_stmt = sub_stmt.with_for_update()
        sub = (await self.session.execute(sub_stmt)).scalar_one_or_none()

        # 2. Level 3: TaskAssignment row
        if sub:
            if task_id:
                stmt = sa.select(TaskAssignment).where(
                    TaskAssignment.submission_id == sub.submission_id,
                    sa.or_(
                        TaskAssignment.assigned_task_id == task_id,
                        TaskAssignment.task_id == task_id,
                    ),
                ).order_by(TaskAssignment.id.desc()).limit(1)
            else:
                per_task_results = list(sub.per_task_results or [])
                submitted_ids = {
                    r.get("task_id") for r in per_task_results if isinstance(r, dict)
                } | {
                    r.get("assigned_task_id") for r in per_task_results if isinstance(r, dict)
                }
                stmt = sa.select(TaskAssignment).where(TaskAssignment.submission_id == sub.submission_id)
                if submitted_ids:
                    stmt = stmt.where(
                        TaskAssignment.task_id.not_in(submitted_ids),
                        sa.or_(
                            TaskAssignment.assigned_task_id.is_(None),
                            TaskAssignment.assigned_task_id.not_in(submitted_ids),
                        ),
                    )
                stmt = stmt.order_by(TaskAssignment.id.asc()).limit(1)
        else:
            stmt = sa.select(TaskAssignment).where(TaskAssignment.team_id == team_id)
            if task_id:
                stmt = stmt.where(
                    sa.or_(
                        TaskAssignment.assigned_task_id == task_id,
                        TaskAssignment.task_id == task_id,
                    )
                )
            stmt = stmt.order_by(TaskAssignment.id.desc()).limit(1)

        if for_update:
            stmt = stmt.with_for_update()

        result = await self.session.execute(stmt)
        assignment = result.scalar_one_or_none()

        if not assignment and sub and task_id:
            breakdown_data = sub.breakdown or {}
            task_plan = breakdown_data.get("task_plan", [])
            matched = next(
                (
                    p
                    for p in task_plan
                    if p.get("assigned_task_id") == task_id or p.get("canonical_task_id") == task_id
                ),
                None,
            )
            if matched:
                canonical_id = matched["canonical_task_id"]
                task_def = await self.session.get(Task, canonical_id)
                if task_def:
                    now = datetime.now(UTC)
                    assignment = TaskAssignment(
                        team_id=team_id,
                        task_id=canonical_id,
                        assigned_task_id=task_id,
                        submission_id=sub.submission_id,
                        assigned_at=now,
                        world_runtime_state=copy.deepcopy(task_def.world_state_seed),
                    )
                    self.session.add(assignment)
                    await self.session.flush()

        if not assignment:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={
                    "error": "NO_ACTIVE_TASK",
                    "message": "No active task assignment found for team. Start a task before using tools.",
                },
            )

        # Enforce submission lifecycle when assignment is part of a submission
        if assignment.submission_id is not None:
            if not sub:
                sub = await self.session.get(Submission, assignment.submission_id)
            if not sub or sub.status != "in_progress":
                sub_status = sub.status if sub else "unknown"
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail={
                        "error": "SUBMISSION_NOT_ACTIVE",
                        "message": f"Submission is {sub_status}. Tool calls are only permitted during an active in-progress submission.",
                    },
                )

            # Check if task was already submitted or timed out
            for r in sub.per_task_results or []:
                if isinstance(r, dict) and r.get("task_id") == assignment.task_id:
                    if r.get("status") == "completed":
                        raise HTTPException(
                            status_code=status.HTTP_409_CONFLICT,
                            detail={
                                "error": "TASK_ALREADY_SUBMITTED",
                                "message": f"Task '{assignment.task_id}' has already been submitted. Start the next task to continue.",
                            },
                        )
                    elif r.get("status") == "timed_out":
                        raise HTTPException(
                            status_code=status.HTTP_409_CONFLICT,
                            detail={
                                "error": "TASK_TIMED_OUT",
                                "message": f"Task '{assignment.task_id}' has timed out. Start the next task to continue.",
                            },
                        )

            # Check time budget against server time
            time_budget = await self.settings_service.get("time_budget_per_task_seconds", 180)
            now = datetime.now(UTC)
            assign_time = (
                assignment.assigned_at
                if assignment.assigned_at.tzinfo is not None
                else assignment.assigned_at.replace(tzinfo=UTC)
            )
            elapsed = (now - assign_time).total_seconds()
            if elapsed > time_budget:
                per_task_results = list(sub.per_task_results or [])
                if not any(r.get("task_id") == assignment.task_id for r in per_task_results if isinstance(r, dict)):
                    per_task_results.append(
                        {
                            "task_id": assignment.task_id,
                            "status": "timed_out",
                            "assigned_at": assignment.assigned_at.isoformat(),
                            "timed_out_at": now.isoformat(),
                        }
                    )
                    sub.per_task_results = per_task_results
                    flag_modified(sub, "per_task_results")
                    await self.session.commit()
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail={
                        "error": "TASK_TIMED_OUT",
                        "message": f"Task '{assignment.task_id}' exceeded the time budget of {time_budget}s (elapsed: {int(elapsed)}s).",
                    },
                )

        return assignment

    async def assign_task(
        self,
        team_id: uuid.UUID,
        task_id: str,
        submission_id: uuid.UUID | None = None,
    ) -> TaskAssignment:
        """Assigns a task to a team, copying tasks.world_state_seed to task_assignments.world_runtime_state."""
        task_res = await self.session.execute(sa.select(Task).where(Task.task_id == task_id))
        task = task_res.scalar_one_or_none()
        if not task:
            raise ValueError(f"Task '{task_id}' does not exist in tasks table.")

        # Create isolated mutable runtime state
        runtime_state = copy.deepcopy(task.world_state_seed)

        assignment = TaskAssignment(
            team_id=team_id,
            task_id=task_id,
            submission_id=submission_id,
            assigned_at=datetime.now(UTC),
            world_runtime_state=runtime_state,
        )
        self.session.add(assignment)
        await self.session.commit()
        await self.session.refresh(assignment)
        return assignment

    async def get_retrieved_evidence_ids(
        self,
        team_id: uuid.UUID,
        task_id: str,
    ) -> set[str]:
        """Extracts all evidence/entity IDs returned to this team in this task via tool_call_logs."""
        stmt = (
            sa.select(ToolCallLog)
            .where(
                ToolCallLog.team_id == team_id,
                ToolCallLog.task_id == task_id,
            )
            .order_by(ToolCallLog.created_at.asc())
        )
        logs = (await self.session.execute(stmt)).scalars().all()

        retrieved: set[str] = set()
        for log in logs:
            resp = log.response_payload or {}
            # Check results from search_knowledge
            for item in resp.get("results", []):
                if isinstance(item, dict) and "id" in item:
                    retrieved.add(item["id"])

            # Check document from get_document
            doc = resp.get("document")
            if isinstance(doc, dict) and "id" in doc:
                retrieved.add(doc["id"])

            # Check customer from get_customer
            cust = resp.get("customer")
            if isinstance(cust, dict) and "id" in cust:
                retrieved.add(cust["id"])

            # Check transactions from get_transactions
            for tx in resp.get("transactions", []):
                if isinstance(tx, dict):
                    if "id" in tx:
                        retrieved.add(tx["id"])
                    if "invoice_id" in tx:
                        retrieved.add(tx["invoice_id"])

            # Check subscription from get_subscription
            sub = resp.get("subscription")
            if isinstance(sub, dict) and "id" in sub:
                retrieved.add(sub["id"])

            # Check cases from get_previous_cases
            for c in resp.get("cases", []):
                if isinstance(c, dict):
                    if "case_id" in c:
                        retrieved.add(c["case_id"])
                    if "id" in c:
                        retrieved.add(c["id"])
                    for eid in c.get("evidence_used", []):
                        retrieved.add(eid)

            # Check transaction from issue_refund
            tx_ref = resp.get("transaction")
            if isinstance(tx_ref, dict) and "id" in tx_ref:
                retrieved.add(tx_ref["id"])

            # Check policy_ref from ineligibility response
            if resp.get("policy_ref"):
                retrieved.add(resp["policy_ref"])

        return retrieved

    # =========================================================================
    # Read Tool Implementations (Pure state reads against runtime state)
    # =========================================================================

    def execute_search_knowledge(
        self,
        world_state: dict[str, Any],
        query: str,
        top_k: int,
    ) -> dict[str, Any]:
        """Searches policies and documents in current world runtime state."""
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
            # Exact substring match bonus
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
                # Snippet truncation
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

        # Sort by score desc, then updated_at desc, then doc_id asc for deterministic tie-breaking
        scored_docs.sort(key=lambda x: (-x[0], -x[1].timestamp(), x[2]))
        results = [item[3] for item in scored_docs[:top_k]]
        return {"results": results}

    def execute_get_document(
        self,
        world_state: dict[str, Any],
        document_id: str,
    ) -> dict[str, Any]:
        """Fetches full policy or document by document_id."""
        for pol in world_state.get("policies", []):
            if pol.get("id") == document_id:
                return {"document": pol}
        for doc in world_state.get("documents", []):
            if doc.get("id") == document_id:
                return {"document": doc}

        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "error": "DOCUMENT_NOT_FOUND",
                "message": f"Document '{document_id}' not found in task knowledge base.",
            },
        )

    def execute_get_customer(
        self,
        world_state: dict[str, Any],
        customer_id: str,
    ) -> dict[str, Any]:
        """Fetches customer record by customer_id."""
        for cust in world_state.get("customers", []):
            if cust.get("id") == customer_id:
                return {"customer": cust}

        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "error": "CUSTOMER_NOT_FOUND",
                "message": f"Customer '{customer_id}' not found in account records.",
            },
        )

    def execute_get_transactions(
        self,
        world_state: dict[str, Any],
        customer_id: str,
        start_date: str | None,
        end_date: str | None,
    ) -> dict[str, Any]:
        """Fetches customer transactions with optional inclusive date bounds."""
        # 1. Verify customer exists
        cust_exists = any(c.get("id") == customer_id for c in world_state.get("customers", []))
        if not cust_exists:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={
                    "error": "CUSTOMER_NOT_FOUND",
                    "message": f"Customer '{customer_id}' not found.",
                },
            )

        # 2. Parse date bounds if provided
        start_dt = None
        end_dt = None
        if start_date:
            try:
                start_dt = datetime.fromisoformat(start_date.replace("Z", "+00:00"))
                if start_dt.tzinfo is None:
                    start_dt = start_dt.replace(tzinfo=UTC)
                start_dt = start_dt.astimezone(UTC)
            except Exception as e:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                    detail={"error": "INVALID_DATE_FORMAT", "message": f"Invalid start_date '{start_date}': {e}"},
                )
        if end_date:
            try:
                end_dt = datetime.fromisoformat(end_date.replace("Z", "+00:00"))
                if end_dt.tzinfo is None:
                    end_dt = end_dt.replace(tzinfo=UTC)
                end_dt = end_dt.astimezone(UTC)
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

        # 3. Filter customer transactions
        txs = [t for t in world_state.get("transactions", []) if t.get("customer_id") == customer_id]
        filtered = []
        for t in txs:
            t_dt = parse_iso(t.get("date", "1970-01-01T00:00:00Z"))
            if start_dt and t_dt < start_dt:
                continue
            if end_dt and t_dt > end_dt:
                continue
            filtered.append(t)

        # Sort descending by date
        filtered.sort(key=lambda t: parse_iso(t.get("date", "1970-01-01T00:00:00Z")), reverse=True)
        return {"transactions": filtered}

    def execute_get_subscription(
        self,
        world_state: dict[str, Any],
        customer_id: str,
    ) -> dict[str, Any]:
        """Fetches customer subscription or null if customer has no subscription."""
        cust_exists = any(c.get("id") == customer_id for c in world_state.get("customers", []))
        if not cust_exists:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={
                    "error": "CUSTOMER_NOT_FOUND",
                    "message": f"Customer '{customer_id}' not found.",
                },
            )

        for sub in world_state.get("subscriptions", []):
            if sub.get("customer_id") == customer_id:
                return {"subscription": sub}

        return {"subscription": None}

    def execute_get_previous_cases(
        self,
        world_state: dict[str, Any],
        customer_id: str,
        limit: int,
    ) -> dict[str, Any]:
        """Fetches historical cases for customer."""
        cust_exists = any(c.get("id") == customer_id for c in world_state.get("customers", []))
        if not cust_exists:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={
                    "error": "CUSTOMER_NOT_FOUND",
                    "message": f"Customer '{customer_id}' not found.",
                },
            )

        cases = [c for c in world_state.get("historical_cases", []) if c.get("customer_id") == customer_id]
        cases.sort(key=lambda c: parse_iso(c.get("date", "1970-01-01T00:00:00Z")), reverse=True)
        return {"cases": cases[:limit]}

    # =========================================================================
    # Action Tool Implementations (Server-side enforced & atomic state mutation)
    # =========================================================================

    def execute_issue_refund(
        self,
        world_state: dict[str, Any],
        transaction_id: str,
        amount: float,
        reason: str,
    ) -> tuple[dict[str, Any] | None, dict[str, Any], bool]:
        """Enforces refund rules via canonical domain check.

        Returns (mutated_world_state_or_None, response_dict, was_rejection).
        """
        result = check_refund_eligibility(world_state, transaction_id, amount, reason)
        if result.error == "TRANSACTION_NOT_FOUND":
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={
                    "error": "TRANSACTION_NOT_FOUND",
                    "message": f"Transaction '{transaction_id}' does not exist in account records.",
                },
            )

        if not result.is_eligible:
            resp = IneligibleResponse(
                error="INELIGIBLE",
                reason=result.reason or "ineligible_action",
                policy_ref=result.policy_ref,
            ).model_dump()
            return None, resp, True

        # Action is eligible: apply state mutation
        mutated, _ = apply_action_to_world(
            world_state,
            action_type="issue_refund",
            params={"transaction_id": transaction_id, "amount": amount, "reason": reason},
        )
        updated_tx = next(t for t in mutated["transactions"] if t.get("id") == transaction_id)
        resp = RefundSuccessResponse(
            status=updated_tx.get("refund_status", "refunded"), transaction=updated_tx
        ).model_dump()
        return mutated, resp, False

    def execute_cancel_subscription(
        self,
        world_state: dict[str, Any],
        customer_id: str,
        subscription_id: str,
    ) -> tuple[dict[str, Any] | None, dict[str, Any], bool]:
        """Enforces cancellation rules via canonical domain check."""
        cust_exists = any(c.get("id") == customer_id for c in world_state.get("customers", []) if isinstance(c, dict))
        if not cust_exists:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={
                    "error": "CUSTOMER_NOT_FOUND",
                    "message": f"Customer '{customer_id}' not found.",
                },
            )

        result = check_cancellation_eligibility(world_state, customer_id, subscription_id)
        if result.error == "SUBSCRIPTION_NOT_FOUND":
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={
                    "error": "SUBSCRIPTION_NOT_FOUND",
                    "message": f"Subscription '{subscription_id}' does not exist.",
                },
            )

        if not result.is_eligible:
            resp = IneligibleResponse(
                error="INELIGIBLE",
                reason=result.reason or "ineligible_action",
                policy_ref=result.policy_ref,
            ).model_dump()
            return None, resp, True

        mutated, _ = apply_action_to_world(
            world_state,
            action_type="cancel_subscription",
            params={"customer_id": customer_id, "subscription_id": subscription_id},
        )
        updated_sub = next(s for s in mutated["subscriptions"] if s["id"] == subscription_id)
        resp = CancelSubscriptionSuccessResponse(status="cancelled", subscription=updated_sub).model_dump()
        return mutated, resp, False

    def execute_escalate_case(
        self,
        world_state: dict[str, Any],
        case_id: str,
        team: str,
        reason: str,
        retrieved_evidence_ids: set[str],
    ) -> tuple[dict[str, Any] | None, dict[str, Any], bool]:
        """Enforces evidence grounding check for escalation."""
        result = check_escalation_validity(
            world_state=world_state,
            case_id=case_id,
            team=team,
            reason=reason,
            retrieved_evidence_ids=list(retrieved_evidence_ids),
        )
        if not result.is_eligible:
            resp = InvalidEscalationResponse(
                error="INVALID_ESCALATION",
                reason=result.reason or "reason_not_grounded",
            ).model_dump()
            return None, resp, True

        mutated, _ = apply_action_to_world(
            world_state,
            action_type="escalate_case",
            params={"case_id": case_id, "team": team, "reason": reason},
            retrieved_evidence_ids=list(retrieved_evidence_ids),
        )
        resp = EscalateCaseSuccessResponse(status="escalated").model_dump()
        return mutated, resp, False

    def execute_request_verification(
        self,
        world_state: dict[str, Any],
        customer_id: str,
        verification_type: str,
    ) -> tuple[dict[str, Any] | None, dict[str, Any], bool]:
        """Safe fallback action; always succeeds when customer exists."""
        cust_exists = any(c.get("id") == customer_id for c in world_state.get("customers", []))
        if not cust_exists:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={
                    "error": "CUSTOMER_NOT_FOUND",
                    "message": f"Customer '{customer_id}' not found.",
                },
            )

        mutated, _ = apply_action_to_world(
            world_state,
            action_type="request_verification",
            params={"customer_id": customer_id, "verification_type": verification_type},
        )
        resp = RequestVerificationSuccessResponse(status="verification_requested").model_dump()
        return mutated, resp, False

    @staticmethod
    def _scrub_payload(data: Any) -> Any:
        """Recursively sanitizes and scrubs bearer tokens and sensitive credentials."""
        if isinstance(data, dict):
            scrubbed = {}
            for k, v in data.items():
                k_lower = str(k).lower()
                if any(s in k_lower for s in ("token", "secret", "auth", "password", "bearer")):
                    scrubbed[k] = "[REDACTED]"
                else:
                    scrubbed[k] = ToolService._scrub_payload(v)
            return scrubbed
        elif isinstance(data, list):
            return [ToolService._scrub_payload(item) for item in data]
        elif isinstance(data, str):
            if "bearer " in data.lower() or (len(data) > 30 and data.startswith("ey")):
                return "[REDACTED]"
            return data
        return data

    # =========================================================================
    # Pipeline Orchestrator (Rate limit + Lock + Execution + Audit Logging)
    # =========================================================================

    async def run_tool(
        self,
        team: Team,
        tool_name: str,
        payload: dict[str, Any],
        is_action: bool = False,
        task_id: str | None = None,
    ) -> dict[str, Any]:
        """Runs a tool call through rate limiting, row locking, enforcement, mutation, and logging."""
        t0 = time.perf_counter()

        team_lock = await get_team_lock(team.team_id)
        async with team_lock:
            try:
                # 1. Resolve active assignment with row-level lock in PostgreSQL (FOR UPDATE)
                # Acquired FIRST inside the transaction so all subsequent queries (rate limit, budget, state)
                # are strictly serialized across all worker processes/instances.
                assignment = await self.get_active_assignment(team.team_id, task_id=task_id, for_update=True)

                # 2. Consolidated team rate limit & task budget check (protected by row lock, single query)
                await ToolLimiter.check_limits(self.session, team.team_id, assignment.task_id, self.settings_service)

                world_runtime = assignment.world_runtime_state
                mutated_world = None
                was_rejection = False
                response_dict: dict[str, Any] = {}

                # 4. Tool Execution
                if tool_name == "search_knowledge":
                    response_dict = self.execute_search_knowledge(
                        world_runtime,
                        query=payload["query"],
                        top_k=payload.get("top_k", 5),
                    )
                elif tool_name == "get_document":
                    response_dict = self.execute_get_document(
                        world_runtime,
                        document_id=payload["document_id"],
                    )
                elif tool_name == "get_customer":
                    response_dict = self.execute_get_customer(
                        world_runtime,
                        customer_id=payload["customer_id"],
                    )
                elif tool_name == "get_transactions":
                    response_dict = self.execute_get_transactions(
                        world_runtime,
                        customer_id=payload["customer_id"],
                        start_date=payload.get("start_date"),
                        end_date=payload.get("end_date"),
                    )
                elif tool_name == "get_subscription":
                    response_dict = self.execute_get_subscription(
                        world_runtime,
                        customer_id=payload["customer_id"],
                    )
                elif tool_name == "get_previous_cases":
                    response_dict = self.execute_get_previous_cases(
                        world_runtime,
                        customer_id=payload["customer_id"],
                        limit=payload.get("limit", 5),
                    )
                elif tool_name == "issue_refund":
                    mutated_world, response_dict, was_rejection = self.execute_issue_refund(
                        world_runtime,
                        transaction_id=payload["transaction_id"],
                        amount=payload["amount"],
                        reason=payload["reason"],
                    )
                elif tool_name == "cancel_subscription":
                    mutated_world, response_dict, was_rejection = self.execute_cancel_subscription(
                        world_runtime,
                        customer_id=payload["customer_id"],
                        subscription_id=payload["subscription_id"],
                    )
                elif tool_name == "escalate_case":
                    retrieved_ids = await self.get_retrieved_evidence_ids(team.team_id, assignment.task_id)
                    mutated_world, response_dict, was_rejection = self.execute_escalate_case(
                        world_runtime,
                        case_id=payload["case_id"],
                        team=payload["team"],
                        reason=payload["reason"],
                        retrieved_evidence_ids=retrieved_ids,
                    )
                elif tool_name == "request_verification":
                    mutated_world, response_dict, was_rejection = self.execute_request_verification(
                        world_runtime,
                        customer_id=payload["customer_id"],
                        verification_type=payload.get("verification_type", "identity"),
                    )
                else:
                    raise HTTPException(
                        status_code=status.HTTP_400_BAD_REQUEST,
                        detail={"error": "UNKNOWN_TOOL", "message": f"Unknown tool '{tool_name}'"},
                    )

                # 5. Apply state mutation only on eligible action
                if is_action and not was_rejection and mutated_world is not None:
                    assignment.world_runtime_state = mutated_world
                    flag_modified(assignment, "world_runtime_state")

                # 6. Record tool_call_logs inside the same transaction boundary
                latency_ms = max(1, int((time.perf_counter() - t0) * 1000))
                scrubbed_req = self._scrub_payload(payload)
                log_entry = ToolCallLog(
                    team_id=team.team_id,
                    task_id=assignment.task_id,
                    submission_id=assignment.submission_id,
                    tool_name=tool_name,
                    request_payload=scrubbed_req,
                    response_payload=response_dict,
                    was_enforcement_rejection=was_rejection,
                    latency_ms=latency_ms,
                    created_at=datetime.now(UTC),
                )
                self.session.add(log_entry)
                await self.session.commit()

                return response_dict
            except Exception:
                await self.session.rollback()
                raise
