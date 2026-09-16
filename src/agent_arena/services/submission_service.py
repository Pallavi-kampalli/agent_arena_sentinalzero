import copy
import random
import secrets
import uuid
from collections import Counter
from datetime import UTC, datetime
from typing import Any

import sqlalchemy as sa
from fastapi import HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import flag_modified

from agent_arena.models.submission import Submission
from agent_arena.models.task import Task
from agent_arena.models.task_assignment import TaskAssignment
from agent_arena.models.team import Team
from agent_arena.models.tool_call_log import ToolCallLog
from agent_arena.schemas.submission import BatchTaskSubmitItem, TaskSubmitRequest
from agent_arena.services.locks import get_team_lock
from agent_arena.services.settings_service import SettingsService


def ensure_utc(dt: datetime) -> datetime:
    """Ensures a datetime object is timezone-aware in UTC."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


class SubmissionService:
    """Service managing the complete task and submission lifecycle.

    Guarantees:
    - Finite state machine with persistent database transitions
    - Strict separation of Task Timeout (per-task) vs Submission Expiration (event-level)
    - Strict object-level authorization (IDOR immunity)
    - Reusable benchmark task definitions (Model B) with per-team isolated runtime states
    - Monotonic, server-controlled attempt numbering (only completed attempts consume limit)
    - Ephemeral randomized task IDs generated on the fly per submission run
    - Inter-task timing calculation and submission tool-call auditing
    - Dynamic enforcement of live settings (submission limit, task count, time budgets)
    - Zero production oracle leakage
    - Strict hierarchical deadlock-free lock acquisition:
      Level 1: Team -> Level 2: Submission -> Level 3: TaskAssignment
    """

    def __init__(self, session: AsyncSession, settings_service: SettingsService):
        self.session = session
        self.settings_service = settings_service

    async def _check_competition_window(self, submission: Submission | None = None) -> None:
        """Verifies that the competition phase and window permit submissions.

        If the event window has closed or phase is frozen, transitions active submission to 'expired'.
        """
        phase = await self.settings_service.get("competition_phase", "build")
        if phase in ("frozen", "evaluating", "results_published"):
            if submission and submission.status == "in_progress":
                submission.status = "expired"
                await self.session.commit()
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "error": "COMPETITION_FROZEN",
                    "message": f"Competition is in '{phase}' phase. Submissions are closed.",
                },
            )

        comp_end = await self.settings_service.get("competition_end_at", None)
        if comp_end:
            now = datetime.now(UTC)
            if isinstance(comp_end, str):
                end_dt = datetime.fromisoformat(comp_end.replace("Z", "+00:00"))
            else:
                end_dt = comp_end
            if ensure_utc(end_dt) < now:
                if submission and submission.status == "in_progress":
                    submission.status = "expired"
                    await self.session.commit()
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail={
                        "error": "COMPETITION_EXPIRED",
                        "message": "Competition end time has passed. Submissions are closed.",
                    },
                )

    async def start_submission(self, team_id: uuid.UUID) -> dict[str, Any]:
        """Starts a new submission run for an authenticated team.

        Delivers all tasks at once in randomized order with on-the-fly generated ephemeral task IDs.
        If an active run is older than 30 minutes, closes it as 'interrupted' without penalty.
        """
        team_lock = await get_team_lock(team_id)
        async with team_lock:
            # Check competition event window
            await self._check_competition_window()

            # 1. Level 1 Lock: Team row
            team_stmt = sa.select(Team).where(Team.team_id == team_id).with_for_update()
            team = (await self.session.execute(team_stmt)).scalar_one_or_none()
            if not team:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail={"error": "TEAM_NOT_FOUND", "message": "Team does not exist."},
                )
            if team.status != "active":
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail={"error": "FORBIDDEN", "message": f"Team is {team.status}."},
                )

            now = datetime.now(UTC)

            # 2. Check if team already has an in_progress submission
            active_sub_stmt = sa.select(Submission).where(
                Submission.team_id == team_id, Submission.status == "in_progress"
            )
            active_sub = (await self.session.execute(active_sub_stmt)).scalar_one_or_none()
            if active_sub:
                elapsed = (now - ensure_utc(active_sub.started_at)).total_seconds()
                if elapsed > 1800:
                    # Older than 30 minutes -> auto-close as interrupted without consuming attempt
                    active_sub.status = "interrupted"
                    active_sub.completed_at = now
                    await self.session.commit()
                else:
                    raise HTTPException(
                        status_code=status.HTTP_409_CONFLICT,
                        detail={
                            "error": "ACTIVE_SUBMISSION_EXISTS",
                            "message": f"An active submission '{active_sub.submission_id}' is already in progress ({int(elapsed)}s elapsed).",
                            "submission_id": str(active_sub.submission_id),
                        },
                    )

            # 3. Check live submission_limit_per_team setting (only completed submissions consume limit)
            limit = await self.settings_service.get("submission_limit_per_team", 5)
            count_stmt = sa.select(sa.func.count()).select_from(Submission).where(
                Submission.team_id == team_id, Submission.status == "completed"
            )
            existing_count = (await self.session.execute(count_stmt)).scalar() or 0
            if existing_count >= limit:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail={
                        "error": "SUBMISSION_LIMIT_EXCEEDED",
                        "message": f"Submission limit of {limit} reached for this team. You cannot start more submissions.",
                    },
                )

            # 4. Check hidden task pool sufficiency
            hidden_count = await self.settings_service.get("hidden_task_count", 30)
            pool_stmt = sa.select(Task).where(Task.dataset == "hidden").order_by(Task.task_id.asc())
            hidden_tasks = list((await self.session.execute(pool_stmt)).scalars().all())
            if len(hidden_tasks) < hidden_count:
                raise HTTPException(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                    detail={
                        "error": "TASK_POOL_EXHAUSTED",
                        "message": f"Task pool has insufficient hidden tasks ({len(hidden_tasks)} available, {hidden_count} required).",
                    },
                )

            # Select required number and randomize their order
            selected_tasks = list(hidden_tasks[:hidden_count])
            random.shuffle(selected_tasks)

            # 5. Monotonic attempt number & atomic creation
            attempt_number = existing_count + 1
            submission_id = uuid.uuid4()
            submission = Submission(
                submission_id=submission_id,
                team_id=team_id,
                attempt_number=attempt_number,
                started_at=now,
                status="in_progress",
                per_task_results=[],
            )
            self.session.add(submission)

            # 6. Generate ephemeral randomized task IDs and create isolated TaskAssignments
            short_sub = str(submission_id)[:8].upper()
            tasks_for_response = []
            for idx, task_def in enumerate(selected_tasks, 1):
                rand_hex = secrets.token_hex(2).upper()
                assigned_task_id = f"TASK-{short_sub}-{idx:02d}-{rand_hex}"

                assignment = TaskAssignment(
                    team_id=team_id,
                    task_id=task_def.task_id,
                    assigned_task_id=assigned_task_id,
                    submission_id=submission_id,
                    assigned_at=now,
                    world_runtime_state=copy.deepcopy(task_def.world_state_seed),
                )
                self.session.add(assignment)

                tasks_for_response.append({
                    "task_id": assigned_task_id,
                    "customer_id": task_def.input_payload.get("customer_id", ""),
                    "customer_message": task_def.input_payload.get("customer_message", ""),
                })

            await self.session.commit()

            return {
                "submission_id": str(submission_id),
                "attempt_number": attempt_number,
                "tasks_total": len(selected_tasks),
                "tasks": tasks_for_response,
            }

    async def start_next_task(self, team_id: uuid.UUID) -> dict[str, Any]:
        """Backwards-compatible task dispenser for legacy contract tests.

        If batch assignments were already generated, yields the next uncompleted task.
        """
        team_lock = await get_team_lock(team_id)
        async with team_lock:
            # 1. Level 2 Lock: Active Submission row
            sub_stmt = (
                sa.select(Submission)
                .where(Submission.team_id == team_id, Submission.status == "in_progress")
                .with_for_update()
            )
            submission = (await self.session.execute(sub_stmt)).scalar_one_or_none()
            if not submission:
                last_sub = (
                    await self.session.execute(
                        sa.select(Submission)
                        .where(Submission.team_id == team_id)
                        .order_by(Submission.started_at.desc())
                        .limit(1)
                    )
                ).scalar_one_or_none()
                if last_sub and last_sub.status == "completed":
                    raise HTTPException(
                        status_code=status.HTTP_409_CONFLICT,
                        detail={
                            "error": "SUBMISSION_ALREADY_FINALIZED",
                            "message": "Submission has been finalized. Start a new submission to continue.",
                        },
                    )
                elif last_sub and last_sub.status in ("expired", "interrupted"):
                    raise HTTPException(
                        status_code=status.HTTP_409_CONFLICT,
                        detail={"error": "SUBMISSION_EXPIRED", "message": f"Submission is {last_sub.status}."},
                    )
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail={"error": "NO_ACTIVE_SUBMISSION", "message": "No active submission in progress."},
                )

            await self._check_competition_window(submission)

            # Check pre-generated assignments for this submission
            assigns_stmt = (
                sa.select(TaskAssignment)
                .where(TaskAssignment.submission_id == submission.submission_id)
                .order_by(TaskAssignment.id.asc())
            )
            existing_assignments = list((await self.session.execute(assigns_stmt)).scalars().all())

            per_task_results = list(submission.per_task_results or [])
            submitted_ids = {
                r.get("task_id") for r in per_task_results if isinstance(r, dict)
            } | {
                r.get("assigned_task_id") for r in per_task_results if isinstance(r, dict)
            }

            if existing_assignments:
                next_assign = None
                for a in existing_assignments:
                    if a.task_id not in submitted_ids and a.assigned_task_id not in submitted_ids:
                        next_assign = a
                        break

                if not next_assign:
                    raise HTTPException(
                        status_code=status.HTTP_400_BAD_REQUEST,
                        detail={
                            "error": "ALL_TASKS_COMPLETED",
                            "message": "All tasks for this submission have been completed. Finalize your submission.",
                        },
                    )

                task_def = await self.session.get(Task, next_assign.task_id)
                disp_id = next_assign.assigned_task_id or next_assign.task_id
                return {
                    "task_id": disp_id,
                    "customer_message": task_def.input_payload.get("customer_message", "") if task_def else "",
                    "customer_id": task_def.input_payload.get("customer_id", "") if task_def else "",
                }

            # Fallback legacy on-demand assignment creation
            hidden_count = await self.settings_service.get("hidden_task_count", 30)
            assigned_task_ids_subq = sa.select(TaskAssignment.task_id).where(
                TaskAssignment.submission_id == submission.submission_id
            )
            next_task_stmt = (
                sa.select(Task)
                .where(Task.dataset == "hidden", Task.task_id.not_in(assigned_task_ids_subq))
                .order_by(Task.task_id.asc())
                .limit(1)
            )
            next_task = (await self.session.execute(next_task_stmt)).scalar_one_or_none()
            if not next_task:
                raise HTTPException(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                    detail={"error": "TASK_POOL_EXHAUSTED", "message": "No unassigned hidden tasks available."},
                )

            now = datetime.now(UTC)
            new_assignment = TaskAssignment(
                team_id=team_id,
                task_id=next_task.task_id,
                assigned_task_id=next_task.task_id,
                submission_id=submission.submission_id,
                assigned_at=now,
                world_runtime_state=copy.deepcopy(next_task.world_state_seed),
            )
            self.session.add(new_assignment)
            await self.session.commit()

            return {
                "task_id": next_task.task_id,
                "customer_message": next_task.input_payload.get("customer_message", ""),
                "customer_id": next_task.input_payload.get("customer_id", ""),
            }

    async def submit_task(self, team_id: uuid.UUID, payload: TaskSubmitRequest) -> dict[str, Any]:
        """Submits agent decision and evidence for a single task (backwards compatibility)."""
        team_lock = await get_team_lock(team_id)
        async with team_lock:
            # 1. Level 2 Lock: Active Submission row
            sub_stmt = (
                sa.select(Submission)
                .where(Submission.team_id == team_id, Submission.status == "in_progress")
                .with_for_update()
            )
            submission = (await self.session.execute(sub_stmt)).scalar_one_or_none()
            if not submission:
                last_sub = (
                    await self.session.execute(
                        sa.select(Submission)
                        .where(Submission.team_id == team_id)
                        .order_by(Submission.started_at.desc())
                        .limit(1)
                    )
                ).scalar_one_or_none()
                if last_sub and last_sub.status == "completed":
                    raise HTTPException(
                        status_code=status.HTTP_409_CONFLICT,
                        detail={"error": "SUBMISSION_ALREADY_FINALIZED", "message": "Submission has been finalized."},
                    )
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail={"error": "NO_ACTIVE_SUBMISSION", "message": "No active submission in progress."},
                )

            await self._check_competition_window(submission)

            # 2. Level 3 Lock: Matching or Latest TaskAssignment row
            assign_stmt = (
                sa.select(TaskAssignment)
                .where(
                    TaskAssignment.submission_id == submission.submission_id,
                    sa.or_(
                        TaskAssignment.assigned_task_id == payload.task_id,
                        TaskAssignment.task_id == payload.task_id,
                    ),
                )
                .order_by(TaskAssignment.id.desc())
                .limit(1)
                .with_for_update()
            )
            assignment = (await self.session.execute(assign_stmt)).scalar_one_or_none()
            if not assignment:
                # Fallback to latest unsubmitted assignment
                latest_stmt = (
                    sa.select(TaskAssignment)
                    .where(TaskAssignment.submission_id == submission.submission_id)
                    .order_by(TaskAssignment.id.desc())
                    .limit(1)
                    .with_for_update()
                )
                assignment = (await self.session.execute(latest_stmt)).scalar_one_or_none()

            if not assignment:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail={"error": "NO_ACTIVE_TASK", "message": "No task has been started for this submission."},
                )

            if assignment.task_id != payload.task_id and assignment.assigned_task_id != payload.task_id:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail={
                        "error": "TASK_NOT_FOUND",
                        "message": f"Task '{payload.task_id}' is not the currently active task or does not match active assignment.",
                    },
                )


            per_task_results = list(submission.per_task_results or [])
            for r in per_task_results:
                if isinstance(r, dict) and (
                    r.get("task_id") == assignment.task_id or r.get("assigned_task_id") == payload.task_id
                ):
                    if r.get("status") == "completed":
                        raise HTTPException(
                            status_code=status.HTTP_409_CONFLICT,
                            detail={
                                "error": "TASK_ALREADY_SUBMITTED",
                                "message": f"Task '{payload.task_id}' has already been submitted.",
                            },
                        )

            now = datetime.now(UTC)
            per_task_results.append(
                {
                    "task_id": assignment.task_id,
                    "assigned_task_id": assignment.assigned_task_id or payload.task_id,
                    "status": "completed",
                    "assigned_at": assignment.assigned_at.isoformat(),
                    "submitted_at": now.isoformat(),
                    "submission_payload": payload.model_dump(),
                }
            )
            submission.per_task_results = per_task_results
            flag_modified(submission, "per_task_results")
            await self.session.commit()

            return {"received": True, "task_id": payload.task_id}

    async def submit_batch(
        self,
        team_id: uuid.UUID,
        submission_id: uuid.UUID,
        answers: list[BatchTaskSubmitItem],
    ) -> dict[str, Any]:
        """Submits all answers for a submission epoch at once.

        Calculates per-task durations, inter-task timing intervals, gathers submission tool logs,
        and triggers ScoringService immediately.
        """
        team_lock = await get_team_lock(team_id)
        async with team_lock:
            # 1. Level 2 Lock: Submission row
            sub_stmt = (
                sa.select(Submission)
                .where(Submission.submission_id == submission_id, Submission.team_id == team_id)
                .with_for_update()
            )
            submission = (await self.session.execute(sub_stmt)).scalar_one_or_none()
            if not submission:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail={"error": "SUBMISSION_NOT_FOUND", "message": f"Submission '{submission_id}' not found."},
                )

            if submission.status == "completed":
                return {
                    "submission_id": str(submission.submission_id),
                    "status": "completed",
                    "tasks_received": len(answers),
                    "duration_seconds": 0.0,
                    "aggregate_score": float(submission.aggregate_score) if submission.aggregate_score else None,
                    "breakdown": submission.breakdown,
                    "message": "Submission was already completed.",
                }

            if submission.status in ("expired", "interrupted"):
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail={
                        "error": "SUBMISSION_INACTIVE",
                        "message": f"Submission is {submission.status} and cannot be submitted.",
                    },
                )

            await self._check_competition_window(submission)

            # 2. Load all assignments for this submission
            assign_stmt = (
                sa.select(TaskAssignment)
                .where(TaskAssignment.submission_id == submission_id)
                .order_by(TaskAssignment.id.asc())
            )
            assignments = list((await self.session.execute(assign_stmt)).scalars().all())
            assign_by_assigned_id = {a.assigned_task_id: a for a in assignments if a.assigned_task_id}
            assign_by_canonical_id = {a.task_id: a for a in assignments}

            now = datetime.now(UTC)
            sub_start_dt = ensure_utc(submission.started_at)
            total_duration = max(0.0, (now - sub_start_dt).total_seconds())

            # 3. Process answers and calculate timing
            per_task_results = []
            prev_completed_at = None

            for ans in answers:
                assign = assign_by_assigned_id.get(ans.task_id) or assign_by_canonical_id.get(ans.task_id)
                if not assign:
                    raise HTTPException(
                        status_code=status.HTTP_404_NOT_FOUND,
                        detail={
                            "error": "TASK_NOT_FOUND",
                            "message": f"Task '{ans.task_id}' does not belong to submission '{submission_id}'.",
                        },
                    )

                task_duration = 0.0
                gap = 0.0
                if ans.started_at and ans.completed_at:
                    try:
                        s_dt = datetime.fromisoformat(ans.started_at.replace("Z", "+00:00"))
                        c_dt = datetime.fromisoformat(ans.completed_at.replace("Z", "+00:00"))
                        task_duration = max(0.0, (c_dt - s_dt).total_seconds())
                        if prev_completed_at:
                            gap = max(0.0, (s_dt - prev_completed_at).total_seconds())
                        prev_completed_at = c_dt
                    except Exception:
                        task_duration = 0.0
                elif ans.completed_at and prev_completed_at:
                    try:
                        c_dt = datetime.fromisoformat(ans.completed_at.replace("Z", "+00:00"))
                        task_duration = max(0.0, (c_dt - prev_completed_at).total_seconds())
                        prev_completed_at = c_dt
                    except Exception:
                        pass

                per_task_results.append(
                    {
                        "task_id": assign.task_id,
                        "assigned_task_id": ans.task_id,
                        "status": "completed",
                        "duration_seconds": round(task_duration, 2),
                        "gap_before_seconds": round(gap, 2),
                        "started_at": ans.started_at,
                        "completed_at": ans.completed_at,
                        "submitted_at": now.isoformat(),
                        "submission_payload": ans.model_dump(),
                    }
                )

            submission.per_task_results = per_task_results
            flag_modified(submission, "per_task_results")

            # 4. Gather tool call statistics for this submission
            logs_stmt = sa.select(ToolCallLog).where(ToolCallLog.submission_id == submission_id)
            tool_logs = list((await self.session.execute(logs_stmt)).scalars().all())
            tool_calls_total = len(tool_logs)
            tool_counts = dict(Counter(l.tool_name for l in tool_logs))
            rejections_count = sum(1 for l in tool_logs if l.was_enforcement_rejection)
            avg_latency = (
                round(sum(l.latency_ms for l in tool_logs) / tool_calls_total, 1)
                if tool_calls_total > 0
                else 0.0
            )

            submission.breakdown = {
                "submission_metrics": {
                    "total_duration_seconds": round(total_duration, 2),
                    "tool_calls_total": tool_calls_total,
                    "tool_calls_breakdown": tool_counts,
                    "tool_calls_rejections": rejections_count,
                    "avg_tool_latency_ms": avg_latency,
                }
            }
            flag_modified(submission, "breakdown")

            submission.status = "completed"
            submission.completed_at = now
            await self.session.commit()

            # 5. Immediately trigger scoring via ScoringService
            from agent_arena.scoring.service import ScoringService

            scoring_service = ScoringService(self.session, self.settings_service)
            score_result = await scoring_service.score_submission(submission_id=submission_id, force=True)

            return {
                "submission_id": str(submission.submission_id),
                "status": "completed",
                "tasks_received": len(answers),
                "duration_seconds": round(total_duration, 2),
                "aggregate_score": score_result.aggregate_score,
                "breakdown": score_result.breakdown,
                "message": "Batch submission received and evaluated successfully.",
            }

    async def abort_submission(self, team_id: uuid.UUID, submission_id: uuid.UUID) -> dict[str, Any]:
        """Aborts an active in-progress submission, marking it interrupted so it is not recorded or scored."""
        team_lock = await get_team_lock(team_id)
        async with team_lock:
            sub = await self.session.get(Submission, submission_id)
            if not sub or sub.team_id != team_id:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail={"error": "SUBMISSION_NOT_FOUND", "message": f"Submission '{submission_id}' not found."},
                )
            if sub.status == "in_progress":
                sub.status = "interrupted"
                sub.completed_at = datetime.now(UTC)
                await self.session.commit()
            return {
                "submission_id": str(submission_id),
                "status": "interrupted",
                "message": "Submission aborted and will not be scored or counted.",
            }

    async def get_submission_status(self, team_id: uuid.UUID, submission_id: uuid.UUID) -> dict[str, Any]:
        """Returns submission progress and non-negative server-calculated remaining time."""
        sub = await self.session.get(Submission, submission_id)
        if not sub or sub.team_id != team_id:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={"error": "SUBMISSION_NOT_FOUND", "message": f"Submission '{submission_id}' not found."},
            )

        try:
            await self._check_competition_window(sub)
        except HTTPException:
            pass

        hidden_count = await self.settings_service.get("hidden_task_count", 30)
        time_budget = await self.settings_service.get("time_budget_per_task_seconds", 180)
        now = datetime.now(UTC)

        results = sub.per_task_results or []
        tasks_completed = len([r for r in results if isinstance(r, dict) and r.get("status") == "completed"])

        if sub.status in ("completed", "expired", "interrupted"):
            time_remaining = 0
        else:
            sub_start = ensure_utc(sub.started_at)
            elapsed = (now - sub_start).total_seconds()
            time_remaining = max(0, int(1800 - elapsed))

        return {
            "status": sub.status,
            "tasks_completed": tasks_completed,
            "tasks_total": hidden_count,
            "time_remaining_seconds": time_remaining,
        }

    async def finalize_submission(self, team_id: uuid.UUID, submission_id: uuid.UUID) -> dict[str, Any]:
        """Finalizes an in-progress submission to completed state."""
        team_lock = await get_team_lock(team_id)
        async with team_lock:
            sub_stmt = sa.select(Submission).where(Submission.submission_id == submission_id).with_for_update()
            sub = (await self.session.execute(sub_stmt)).scalar_one_or_none()
            if not sub or sub.team_id != team_id:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail={"error": "SUBMISSION_NOT_FOUND", "message": f"Submission '{submission_id}' not found."},
                )

            if sub.status == "completed":
                return {"submission_id": str(sub.submission_id), "status": "completed"}

            if sub.status in ("expired", "interrupted"):
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail={
                        "error": "SUBMISSION_INACTIVE",
                        "message": f"Submission is {sub.status} and cannot be finalized.",
                    },
                )
            if sub.status != "in_progress":
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail={
                        "error": "INVALID_STATE_TRANSITION",
                        "message": f"Cannot finalize submission in '{sub.status}' state.",
                    },
                )

            now = datetime.now(UTC)
            sub.status = "completed"
            sub.completed_at = now
            await self.session.commit()

            return {"submission_id": str(sub.submission_id), "status": "completed"}
