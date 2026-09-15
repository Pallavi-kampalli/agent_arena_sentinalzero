import copy
import uuid
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
from agent_arena.schemas.submission import TaskSubmitRequest
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
    - Monotonic, server-controlled attempt numbering
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

        Serializes concurrent requests for the same team via shared in-process mutex
        and Level 1 lock on Team row (FOR UPDATE).
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

            # 2. Check if team already has an in_progress submission
            active_sub_stmt = sa.select(Submission).where(
                Submission.team_id == team_id, Submission.status == "in_progress"
            )
            active_sub = (await self.session.execute(active_sub_stmt)).scalar_one_or_none()
            if active_sub:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail={
                        "error": "ACTIVE_SUBMISSION_EXISTS",
                        "message": f"An active submission '{active_sub.submission_id}' is already in progress. Finalize it before starting a new one.",
                        "submission_id": str(active_sub.submission_id),
                    },
                )

            # 3. Check live submission_limit_per_team setting
            limit = await self.settings_service.get("submission_limit_per_team", 5)
            count_stmt = sa.select(sa.func.count()).select_from(Submission).where(Submission.team_id == team_id)
            existing_count = (await self.session.execute(count_stmt)).scalar() or 0
            if existing_count >= limit:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail={
                        "error": "SUBMISSION_LIMIT_EXCEEDED",
                        "message": f"Submission limit of {limit} reached for this team. You cannot start more submissions.",
                    },
                )

            # 4. Check hidden task pool sufficiency (Model B: pool must have at least hidden_task_count task definitions)
            hidden_count = await self.settings_service.get("hidden_task_count", 200)
            pool_stmt = sa.select(sa.func.count()).select_from(Task).where(Task.dataset == "hidden")
            available_tasks = (await self.session.execute(pool_stmt)).scalar() or 0
            if available_tasks < hidden_count:
                raise HTTPException(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                    detail={
                        "error": "TASK_POOL_EXHAUSTED",
                        "message": f"Task pool has insufficient hidden tasks ({available_tasks} available, {hidden_count} required).",
                    },
                )

            # 5. Monotonic attempt number & atomic creation
            attempt_number = existing_count + 1
            submission_id = uuid.uuid4()
            submission = Submission(
                submission_id=submission_id,
                team_id=team_id,
                attempt_number=attempt_number,
                started_at=datetime.now(UTC),
                status="in_progress",
                per_task_results=[],
            )
            self.session.add(submission)
            await self.session.commit()

            return {
                "submission_id": str(submission_id),
                "attempt_number": attempt_number,
                "tasks_total": hidden_count,
            }

    async def start_next_task(self, team_id: uuid.UUID) -> dict[str, Any]:
        """Assigns the next unassigned task for this team's current submission.

        Serializes concurrent task starts for the same submission via in-process mutex
        and Level 2 lock on Submission row (FOR UPDATE).
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
                last_sub_stmt = (
                    sa.select(Submission)
                    .where(Submission.team_id == team_id)
                    .order_by(Submission.started_at.desc())
                    .limit(1)
                )
                last_sub = (await self.session.execute(last_sub_stmt)).scalar_one_or_none()
                if last_sub and last_sub.status == "completed":
                    raise HTTPException(
                        status_code=status.HTTP_409_CONFLICT,
                        detail={
                            "error": "SUBMISSION_ALREADY_FINALIZED",
                            "message": "Submission has been finalized. Start a new submission to continue.",
                        },
                    )
                elif last_sub and last_sub.status == "expired":
                    raise HTTPException(
                        status_code=status.HTTP_409_CONFLICT,
                        detail={
                            "error": "SUBMISSION_EXPIRED",
                            "message": "Submission has expired.",
                        },
                    )
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail={
                        "error": "NO_ACTIVE_SUBMISSION",
                        "message": "No active submission in progress. Call POST /submission/start first.",
                    },
                )

            # Check competition event window
            await self._check_competition_window(submission)

            now = datetime.now(UTC)
            time_budget = await self.settings_service.get("time_budget_per_task_seconds", 180)

            # 2. Level 3 Lock: Latest TaskAssignment row
            latest_assign_stmt = (
                sa.select(TaskAssignment)
                .where(TaskAssignment.submission_id == submission.submission_id)
                .order_by(TaskAssignment.assigned_at.desc(), TaskAssignment.id.desc())
                .limit(1)
                .with_for_update()
            )
            latest_assign = (await self.session.execute(latest_assign_stmt)).scalar_one_or_none()

            per_task_results = list(submission.per_task_results or [])
            submitted_task_ids = {r["task_id"] for r in per_task_results if isinstance(r, dict) and "task_id" in r}

            if latest_assign:
                if latest_assign.task_id not in submitted_task_ids:
                    assign_time = ensure_utc(latest_assign.assigned_at)
                    elapsed = (now - assign_time).total_seconds()
                    if elapsed <= time_budget:
                        # Previous task is still running within its time budget
                        raise HTTPException(
                            status_code=status.HTTP_409_CONFLICT,
                            detail={
                                "error": "TASK_IN_PROGRESS",
                                "message": f"Task '{latest_assign.task_id}' is currently in progress. Complete or await timeout before starting next task.",
                                "task_id": latest_assign.task_id,
                            },
                        )
                    else:
                        # Previous task timed out -> auto-mark timed_out in per_task_results
                        # NOTE: Task timeout does NOT expire the submission; team advances to next task
                        per_task_results.append(
                            {
                                "task_id": latest_assign.task_id,
                                "status": "timed_out",
                                "assigned_at": latest_assign.assigned_at.isoformat(),
                                "timed_out_at": now.isoformat(),
                            }
                        )
                        submission.per_task_results = per_task_results
                        flag_modified(submission, "per_task_results")

            # 3. Check if all tasks have been assigned
            hidden_count = await self.settings_service.get("hidden_task_count", 200)
            assigned_count_stmt = (
                sa.select(sa.func.count())
                .select_from(TaskAssignment)
                .where(TaskAssignment.submission_id == submission.submission_id)
            )
            assigned_count = (await self.session.execute(assigned_count_stmt)).scalar() or 0
            if assigned_count >= hidden_count:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail={
                        "error": "ALL_TASKS_COMPLETED",
                        "message": f"All {hidden_count} tasks for this submission have been assigned. Finalize your submission.",
                    },
                )

            # 4. Pick next unassigned task from hidden pool (Model B: reusable tasks, scoped to THIS submission)
            assigned_task_ids_subq = sa.select(TaskAssignment.task_id).where(
                TaskAssignment.submission_id == submission.submission_id
            )
            next_task_stmt = (
                sa.select(Task)
                .where(
                    Task.dataset == "hidden",
                    Task.task_id.not_in(assigned_task_ids_subq),
                )
                .order_by(Task.task_id.asc())
                .limit(1)
            )
            next_task = (await self.session.execute(next_task_stmt)).scalar_one_or_none()
            if not next_task:
                raise HTTPException(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                    detail={"error": "TASK_POOL_EXHAUSTED", "message": "No unassigned hidden tasks available."},
                )

            # 5. Create new TaskAssignment with isolated deep copy of world_state_seed
            new_assignment = TaskAssignment(
                team_id=team_id,
                task_id=next_task.task_id,
                submission_id=submission.submission_id,
                assigned_at=now,
                world_runtime_state=copy.deepcopy(next_task.world_state_seed),
            )
            self.session.add(new_assignment)
            await self.session.commit()

            # Return only participant-visible fields (zero oracle leakage)
            return {
                "task_id": next_task.task_id,
                "customer_message": next_task.input_payload.get("customer_message", ""),
                "customer_id": next_task.input_payload.get("customer_id", ""),
            }

    async def submit_task(self, team_id: uuid.UUID, payload: TaskSubmitRequest) -> dict[str, Any]:
        """Submits agent decision and evidence for the currently active task.

        Acquires locks in strict hierarchical order: Level 2 (Submission) -> Level 3 (TaskAssignment).
        Never leaks correctness, diffs, or ground truth in production mode.
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
                        detail={"error": "SUBMISSION_ALREADY_FINALIZED", "message": "Submission has been finalized."},
                    )
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail={"error": "NO_ACTIVE_SUBMISSION", "message": "No active submission in progress."},
                )

            # Check competition event window
            await self._check_competition_window(submission)

            # 2. Level 3 Lock: Latest TaskAssignment row
            assign_stmt = (
                sa.select(TaskAssignment)
                .where(TaskAssignment.submission_id == submission.submission_id)
                .order_by(TaskAssignment.assigned_at.desc(), TaskAssignment.id.desc())
                .limit(1)
                .with_for_update()
            )
            assignment = (await self.session.execute(assign_stmt)).scalar_one_or_none()
            if not assignment:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail={"error": "NO_ACTIVE_TASK", "message": "No task has been started for this submission."},
                )

            # 3. Object-level authorization & task match check (fails closed, no cross-team leakage)
            if assignment.task_id != payload.task_id:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail={
                        "error": "TASK_NOT_FOUND",
                        "message": f"Task '{payload.task_id}' is not the currently active task for this team.",
                    },
                )

            # 4. Check if already submitted or permanently timed out in per_task_results
            per_task_results = list(submission.per_task_results or [])
            for r in per_task_results:
                if isinstance(r, dict) and r.get("task_id") == payload.task_id:
                    if r.get("status") == "completed":
                        raise HTTPException(
                            status_code=status.HTTP_409_CONFLICT,
                            detail={
                                "error": "TASK_ALREADY_SUBMITTED",
                                "message": f"Task '{payload.task_id}' has already been submitted.",
                            },
                        )
                    elif r.get("status") == "timed_out":
                        raise HTTPException(
                            status_code=status.HTTP_409_CONFLICT,
                            detail={
                                "error": "TASK_TIMED_OUT",
                                "message": f"Task '{payload.task_id}' has timed out and cannot be submitted.",
                            },
                        )

            # 5. Check wall-clock time budget against server time
            now = datetime.now(UTC)
            time_budget = await self.settings_service.get("time_budget_per_task_seconds", 180)
            assign_time = ensure_utc(assignment.assigned_at)
            elapsed = (now - assign_time).total_seconds()
            if elapsed > time_budget:
                per_task_results.append(
                    {
                        "task_id": payload.task_id,
                        "status": "timed_out",
                        "assigned_at": assignment.assigned_at.isoformat(),
                        "timed_out_at": now.isoformat(),
                    }
                )
                submission.per_task_results = per_task_results
                flag_modified(submission, "per_task_results")
                await self.session.commit()
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail={
                        "error": "TASK_TIMED_OUT",
                        "message": f"Task '{payload.task_id}' exceeded the time budget of {time_budget}s (elapsed: {int(elapsed)}s).",
                    },
                )

            # 6. Valid completion: record in per_task_results
            per_task_results.append(
                {
                    "task_id": payload.task_id,
                    "status": "completed",
                    "assigned_at": assignment.assigned_at.isoformat(),
                    "submitted_at": now.isoformat(),
                    "submission_payload": payload.model_dump(),
                }
            )
            submission.per_task_results = per_task_results
            flag_modified(submission, "per_task_results")
            await self.session.commit()

            # Production receipt: no correctness, no ground truth
            return {"received": True, "task_id": payload.task_id}

    async def get_submission_status(self, team_id: uuid.UUID, submission_id: uuid.UUID) -> dict[str, Any]:
        """Returns submission progress and non-negative server-calculated remaining time."""
        # 1. Object-level authorization check
        sub = await self.session.get(Submission, submission_id)
        if not sub or sub.team_id != team_id:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={"error": "SUBMISSION_NOT_FOUND", "message": f"Submission '{submission_id}' not found."},
            )

        # Check competition event window
        try:
            await self._check_competition_window(sub)
        except HTTPException:
            pass  # Window closed, status is updated to expired if applicable

        hidden_count = await self.settings_service.get("hidden_task_count", 200)
        time_budget = await self.settings_service.get("time_budget_per_task_seconds", 180)
        now = datetime.now(UTC)

        results = sub.per_task_results or []
        tasks_completed = len([r for r in results if isinstance(r, dict) and r.get("status") == "completed"])

        if sub.status in ("completed", "expired"):
            time_remaining = 0
        else:
            # Active submission: calculate time remaining on active task
            assign_stmt = (
                sa.select(TaskAssignment)
                .where(TaskAssignment.submission_id == submission_id)
                .order_by(TaskAssignment.assigned_at.desc(), TaskAssignment.id.desc())
                .limit(1)
            )
            latest_assign = (await self.session.execute(assign_stmt)).scalar_one_or_none()
            if latest_assign:
                finished_ids = {r["task_id"] for r in results if isinstance(r, dict) and "task_id" in r}
                if latest_assign.task_id not in finished_ids:
                    assign_time = ensure_utc(latest_assign.assigned_at)
                    elapsed = (now - assign_time).total_seconds()
                    time_remaining = max(0, int(time_budget - elapsed))
                else:
                    time_remaining = time_budget
            else:
                time_remaining = time_budget

        return {
            "status": sub.status,
            "tasks_completed": tasks_completed,
            "tasks_total": hidden_count,
            "time_remaining_seconds": time_remaining,
        }

    async def finalize_submission(self, team_id: uuid.UUID, submission_id: uuid.UUID) -> dict[str, Any]:
        """Finalizes an in-progress submission to completed state.

        Acquires locks in strict order: Level 2 (Submission) -> Level 3 (TaskAssignment).
        Idempotent for already-completed submissions.
        """
        team_lock = await get_team_lock(team_id)
        async with team_lock:
            # 1. Level 2 Lock: Submission row
            sub_stmt = sa.select(Submission).where(Submission.submission_id == submission_id).with_for_update()
            sub = (await self.session.execute(sub_stmt)).scalar_one_or_none()
            if not sub or sub.team_id != team_id:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail={"error": "SUBMISSION_NOT_FOUND", "message": f"Submission '{submission_id}' not found."},
                )

            # Idempotent observation of already completed state
            if sub.status == "completed":
                return {"submission_id": str(sub.submission_id), "status": "completed"}

            if sub.status == "expired":
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail={
                        "error": "SUBMISSION_EXPIRED",
                        "message": "Submission has expired and cannot be finalized.",
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
            results = list(sub.per_task_results or [])
            finished_ids = {r["task_id"] for r in results if isinstance(r, dict) and "task_id" in r}

            # 2. Level 3 Lock: TaskAssignment row
            assign_stmt = (
                sa.select(TaskAssignment)
                .where(TaskAssignment.submission_id == submission_id)
                .order_by(TaskAssignment.assigned_at.desc(), TaskAssignment.id.desc())
                .limit(1)
                .with_for_update()
            )
            latest_assign = (await self.session.execute(assign_stmt)).scalar_one_or_none()
            if latest_assign and latest_assign.task_id not in finished_ids:
                # Auto-mark any active unsubmitted task as timed_out
                results.append(
                    {
                        "task_id": latest_assign.task_id,
                        "status": "timed_out",
                        "assigned_at": latest_assign.assigned_at.isoformat(),
                        "timed_out_at": now.isoformat(),
                    }
                )
                sub.per_task_results = results
                flag_modified(sub, "per_task_results")

            sub.status = "completed"
            sub.completed_at = now
            await self.session.commit()

            return {"submission_id": str(sub.submission_id), "status": "completed"}
