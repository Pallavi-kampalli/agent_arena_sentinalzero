import copy
import re
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
    check_escalation_validity,
    parse_iso,
)
from agent_arena.models.submission import Submission
from agent_arena.models.task import Task
from agent_arena.models.task_assignment import TaskAssignment
from agent_arena.models.team import Team
from agent_arena.models.tool_call_log import ToolCallLog
from agent_arena.schemas.tools import (
    AllowAndDeliverResponse,
    ApplyWarningBannerResponse,
    EscalateToTier2SocResponse,
    GetApprovedDomainsResponse,
    GetEmailHeadersResponse,
    GetThreadHistoryResponse,
    InspectDomainReputationResponse,
    InvalidEscalationResponse,
    LookupDirectoryResponse,
    QuarantineMessageResponse,
)
from agent_arena.services.locks import get_team_lock
from agent_arena.services.rate_limiter import ToolLimiter
from agent_arena.services.settings_service import SettingsService


class ToolService:
    """Core tool execution runtime for all 9 SentinelZero tools.

    Guarantees:
    - Team and task runtime isolation (all calls resolve against active TaskAssignment)
    - Monotonic hierarchical row-level locking: Level 1 (Team) -> Level 2 (Submission) -> Level 3 (TaskAssignment)
    - Canonical rule enforcement
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
        """Resolves the currently active TaskAssignment for an authenticated team."""
        sub = None
        sub_stmt = sa.select(Submission).where(Submission.team_id == team_id, Submission.status == "in_progress")
        if for_update:
            sub_stmt = sub_stmt.with_for_update()
        sub = (await self.session.execute(sub_stmt)).scalar_one_or_none()

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
        """Assigns a task to a team."""
        task_res = await self.session.execute(sa.select(Task).where(Task.task_id == task_id))
        task = task_res.scalar_one_or_none()
        if not task:
            raise ValueError(f"Task '{task_id}' does not exist in tasks table.")

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
        """Extracts all evidence IDs (EMP-*, DOM-*, MSG-*, THR-*, POL-*, LOG-*) returned to team in tool_call_logs."""
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
        pattern = re.compile(r"\b(EMP-\w+|DOM-\w+|MSG-\w+|THR-\w+|POL-\w+|LOG-\w+)\b")

        for log in logs:
            resp = log.response_payload or {}
            resp_str = str(resp)
            matches = pattern.findall(resp_str)
            for m in matches:
                retrieved.add(m)

        return retrieved

    # =========================================================================
    # SentinelZero Read Tools (5 Endpoints)
    # =========================================================================

    def execute_lookup_directory(
        self,
        world_state: dict[str, Any],
        identifier: str,
    ) -> dict[str, Any]:
        """Look up an employee by email or employee ID."""
        ident_clean = identifier.strip().lower()
        directory = world_state.get("directory", [])

        for emp in directory:
            if not isinstance(emp, dict):
                continue
            if (
                emp.get("official_email", "").strip().lower() == ident_clean
                or emp.get("id", "").strip().lower() == ident_clean
                or emp.get("name", "").strip().lower() == ident_clean
            ):
                return LookupDirectoryResponse(found=True, employee=emp).model_dump()

        return LookupDirectoryResponse(found=False, employee=None).model_dump()

    def execute_get_approved_domains(
        self,
        world_state: dict[str, Any],
    ) -> dict[str, Any]:
        """Retrieves official and partner domain lists."""
        domains = world_state.get("domains", [])
        official = [d["domain"] for d in domains if isinstance(d, dict) and d.get("category") == "official"]
        partner = [d["domain"] for d in domains if isinstance(d, dict) and d.get("category") == "partner"]
        return GetApprovedDomainsResponse(official_domains=official, partner_domains=partner).model_dump()

    def execute_get_email_headers(
        self,
        world_state: dict[str, Any],
        message_id: str,
    ) -> dict[str, Any]:
        """Inspects email security headers and authentication results."""
        # Find message in world state (check target or threads)
        msg_id_clean = message_id.strip()

        # Check if message is target message or in threads
        matching_msg = None
        target_msg_id = world_state.get("target_message_id")
        
        # Check thread history
        for thr in world_state.get("threads", []):
            if isinstance(thr, dict):
                for m in thr.get("messages", []):
                    if isinstance(m, dict) and m.get("message_id") == msg_id_clean:
                        matching_msg = m
                        break

        # Check directory / domain context to build headers if matching target
        sender = matching_msg.get("sender") if matching_msg else None
        
        # Build synthetic authentication headers based on domain reputation
        from_domain = sender.split("@")[-1] if sender and "@" in sender else "unknown.com"
        
        # Check domain category
        domains = world_state.get("domains", [])
        threat_intel = world_state.get("threat_intel", [])
        
        is_official = any(d.get("domain") == from_domain and d.get("category") == "official" for d in domains if isinstance(d, dict))
        threat_record = next((t for t in threat_intel if isinstance(t, dict) and t.get("domain") == from_domain), None)

        if is_official:
            auth_results = {"spf": "pass", "dkim": "pass", "dmarc": "pass"}
        elif threat_record and threat_record.get("reputation") == "malicious":
            auth_results = {"spf": "fail", "dkim": "fail", "dmarc": "fail"}
        else:
            auth_results = {"spf": "none", "dkim": "none", "dmarc": "none"}

        return GetEmailHeadersResponse(
            message_id=msg_id_clean,
            from_header=sender or f"sender@{from_domain}",
            reply_to=sender or f"sender@{from_domain}",
            return_path=sender or f"sender@{from_domain}",
            originating_ip="192.0.2.45",
            originating_domain=from_domain,
            auth_results=auth_results,
        ).model_dump()

    def execute_inspect_domain_reputation(
        self,
        world_state: dict[str, Any],
        domain: str,
    ) -> dict[str, Any]:
        """Inspects domain reputation from threat intel and domain registry."""
        dom_clean = domain.strip().lower()

        # Check official/partner domains first
        for d in world_state.get("domains", []):
            if isinstance(d, dict) and d.get("domain", "").lower() == dom_clean:
                return InspectDomainReputationResponse(
                    domain=d["domain"],
                    domain_id=d.get("domain_id"),
                    is_registered_internal=(d.get("category") == "official"),
                    domain_age_days=1800,
                    reputation="trusted",
                    lookalike_of=None,
                    threat_score=0,
                    known_tags=["verified_domain"],
                ).model_dump()

        # Check threat intelligence records
        for t in world_state.get("threat_intel", []):
            if isinstance(t, dict) and t.get("domain", "").lower() == dom_clean:
                return InspectDomainReputationResponse(
                    domain=t["domain"],
                    domain_id=t.get("domain_id"),
                    is_registered_internal=False,
                    domain_age_days=t.get("domain_age_days", 1),
                    reputation=t.get("reputation", "suspicious"),
                    lookalike_of=t.get("lookalike_of"),
                    threat_score=t.get("threat_score", 50),
                    known_tags=t.get("known_tags", []),
                ).model_dump()

        # Default unknown external domain (not automatically malicious)
        return InspectDomainReputationResponse(
            domain=dom_clean,
            domain_id=None,
            is_registered_internal=False,
            domain_age_days=30,
            reputation="unknown",
            lookalike_of=None,
            threat_score=10,
            known_tags=["external_unverified"],
        ).model_dump()

    def execute_get_thread_history(
        self,
        world_state: dict[str, Any],
        thread_id: str,
    ) -> dict[str, Any]:
        """Fetches chronological message history for a conversation thread."""
        thr_clean = thread_id.strip()
        threads = world_state.get("threads", [])

        for thr in threads:
            if isinstance(thr, dict) and thr.get("thread_id") == thr_clean:
                msgs = thr.get("messages", [])
                return GetThreadHistoryResponse(
                    thread_id=thr_clean,
                    message_count=len(msgs),
                    messages=msgs,
                ).model_dump()

        # If thread not found in overrides, return empty thread history
        return GetThreadHistoryResponse(
            thread_id=thr_clean,
            message_count=0,
            messages=[],
        ).model_dump()

    # =========================================================================
    # SentinelZero Action Tools (4 Endpoints)
    # =========================================================================

    def execute_allow_and_deliver(
        self,
        world_state: dict[str, Any],
        message_id: str,
        reason: str,
    ) -> tuple[dict[str, Any] | None, dict[str, Any], bool]:
        """Executes allow_and_deliver action."""
        mutated, result = apply_action_to_world(
            world_state,
            action_type="allow_and_deliver",
            params={"message_id": message_id, "reason": reason},
        )
        resp = AllowAndDeliverResponse(status="delivered", message_id=message_id).model_dump()
        return mutated, resp, False

    def execute_apply_warning_banner(
        self,
        world_state: dict[str, Any],
        message_id: str,
        banner_type: str,
        reason: str,
    ) -> tuple[dict[str, Any] | None, dict[str, Any], bool]:
        """Executes apply_warning_banner action."""
        mutated, result = apply_action_to_world(
            world_state,
            action_type="apply_warning_banner",
            params={"message_id": message_id, "banner_type": banner_type, "reason": reason},
        )
        resp = ApplyWarningBannerResponse(
            status="warning_applied", message_id=message_id, banner=banner_type
        ).model_dump()
        return mutated, resp, False

    def execute_quarantine_message(
        self,
        world_state: dict[str, Any],
        message_id: str,
        reason: str,
    ) -> tuple[dict[str, Any] | None, dict[str, Any], bool]:
        """Executes quarantine_message action."""
        mutated, result = apply_action_to_world(
            world_state,
            action_type="quarantine_message",
            params={"message_id": message_id, "reason": reason},
        )
        resp = QuarantineMessageResponse(status="quarantined", message_id=message_id).model_dump()
        return mutated, resp, False

    def execute_escalate_to_tier2_soc(
        self,
        world_state: dict[str, Any],
        message_id: str,
        reason: str,
        retrieved_evidence_ids: set[str],
    ) -> tuple[dict[str, Any] | None, dict[str, Any], bool]:
        """Executes escalate_to_tier2_soc with evidence grounding verification."""
        result = check_escalation_validity(
            world_state=world_state,
            message_id=message_id,
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
            action_type="escalate_to_tier2_soc",
            params={"message_id": message_id, "reason": reason},
            retrieved_evidence_ids=list(retrieved_evidence_ids),
        )
        resp = EscalateToTier2SocResponse(status="escalated_to_soc", message_id=message_id).model_dump()
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
        """Runs a SentinelZero tool call through rate limiting, row locking, execution, and logging."""
        t0 = time.perf_counter()

        team_lock = await get_team_lock(team.team_id)
        async with team_lock:
            try:
                assignment = await self.get_active_assignment(team.team_id, task_id=task_id, for_update=True)
                await ToolLimiter.check_limits(self.session, team.team_id, assignment.task_id, self.settings_service)

                world_runtime = assignment.world_runtime_state
                mutated_world = None
                was_rejection = False
                response_dict: dict[str, Any] = {}

                # Tool Execution Dispatch for SentinelZero
                if tool_name == "lookup_directory":
                    response_dict = self.execute_lookup_directory(
                        world_runtime,
                        identifier=payload["identifier"],
                    )
                elif tool_name == "get_approved_domains":
                    response_dict = self.execute_get_approved_domains(world_runtime)
                elif tool_name == "get_email_headers":
                    response_dict = self.execute_get_email_headers(
                        world_runtime,
                        message_id=payload["message_id"],
                    )
                elif tool_name == "inspect_domain_reputation":
                    response_dict = self.execute_inspect_domain_reputation(
                        world_runtime,
                        domain=payload["domain"],
                    )
                elif tool_name == "get_thread_history":
                    response_dict = self.execute_get_thread_history(
                        world_runtime,
                        thread_id=payload["thread_id"],
                    )
                elif tool_name == "allow_and_deliver":
                    mutated_world, response_dict, was_rejection = self.execute_allow_and_deliver(
                        world_runtime,
                        message_id=payload["message_id"],
                        reason=payload["reason"],
                    )
                elif tool_name == "apply_warning_banner":
                    mutated_world, response_dict, was_rejection = self.execute_apply_warning_banner(
                        world_runtime,
                        message_id=payload["message_id"],
                        banner_type=payload.get("banner_type", "EXTERNAL_SENDER"),
                        reason=payload["reason"],
                    )
                elif tool_name == "quarantine_message":
                    mutated_world, response_dict, was_rejection = self.execute_quarantine_message(
                        world_runtime,
                        message_id=payload["message_id"],
                        reason=payload["reason"],
                    )
                elif tool_name == "escalate_to_tier2_soc":
                    retrieved_ids = await self.get_retrieved_evidence_ids(team.team_id, assignment.task_id)
                    mutated_world, response_dict, was_rejection = self.execute_escalate_to_tier2_soc(
                        world_runtime,
                        message_id=payload["message_id"],
                        reason=payload["reason"],
                        retrieved_evidence_ids=retrieved_ids,
                    )
                else:
                    raise HTTPException(
                        status_code=status.HTTP_400_BAD_REQUEST,
                        detail={"error": "UNKNOWN_TOOL", "message": f"Unknown tool '{tool_name}'"},
                    )

                if is_action and not was_rejection and mutated_world is not None:
                    assignment.world_runtime_state = mutated_world
                    flag_modified(assignment, "world_runtime_state")

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
