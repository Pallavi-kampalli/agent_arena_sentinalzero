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
from agent_arena.models.tool_call_log import ToolCallLog
from agent_arena.scoring.evaluator import DEFAULT_WEIGHTS, TaskEvaluator
from agent_arena.scoring.schemas import (
    DimensionScores,
    SubmissionScoreResult,
    TaskScoreResult,
    TeamScoreSummary,
)
from agent_arena.services.locks import get_submission_lock
from agent_arena.services.settings_service import SettingsService

SCORING_ENGINE_VERSION = "1.0.0"


class ScoringService:
    """Production scoring service managing task and submission evaluation.

    Guarantees:
    - Zero N+1 queries: batched retrieval of tasks, assignments, and logs.
    - Level 2 lock acquisition (Submission FOR UPDATE) preserving global lock hierarchy.
    - In-process mutex (get_submission_lock) preventing concurrent scoring races within process.
    - Deterministic, idempotent scoring.
    - Sealed timeout and early-finalization scoring defense.
    - Model B task reuse support with isolated per-assignment runtime evaluation.
    - Full configuration snapshotting and audit metadata.
    """

    def __init__(self, session: AsyncSession, settings_service: SettingsService):
        self.session = session
        self.settings_service = settings_service

    async def score_submission(
        self,
        submission_id: uuid.UUID,
        force: bool = False,
    ) -> SubmissionScoreResult:
        """Scores a finalized or expired submission.

        Serializes on Level 2 lock (Submission row FOR UPDATE) and in-process submission mutex.
        Idempotent: returns existing score if already computed and not forced.
        """
        sub_lock = await get_submission_lock(submission_id)
        async with sub_lock:
            # 1. Level 2 Lock: Submission row
            sub_stmt = sa.select(Submission).where(Submission.submission_id == submission_id).with_for_update()
            sub = (await self.session.execute(sub_stmt)).scalar_one_or_none()
            if not sub:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail={"error": "SUBMISSION_NOT_FOUND", "message": f"Submission '{submission_id}' not found."},
                )

            if sub.status == "in_progress":
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail={
                        "error": "SUBMISSION_STILL_IN_PROGRESS",
                        "message": "Cannot score a submission that is still in progress. Finalize the submission first.",
                    },
                )

            # Idempotency check
            if sub.aggregate_score is not None and not force and sub.breakdown:
                # Reconstruct and return cached result
                tasks_scored = []
                for r in sub.per_task_results or []:
                    if isinstance(r, dict) and "scores" in r:
                        scores_data = r["scores"]
                        tasks_scored.append(
                            TaskScoreResult(
                                task_id=r.get("task_id", ""),
                                status=r.get("status", "completed"),
                                scores=DimensionScores(**scores_data),
                                audit=r.get("audit", {}),
                            )
                        )
                return SubmissionScoreResult(
                    submission_id=str(sub.submission_id),
                    team_id=str(sub.team_id),
                    attempt_number=sub.attempt_number,
                    status=sub.status,
                    aggregate_score=float(sub.aggregate_score),
                    breakdown=sub.breakdown,
                    tasks_scored=tasks_scored,
                )

            # 2. Load live scoring configuration snapshot
            weights = await self.settings_service.get("scoring_weights", DEFAULT_WEIGHTS)
            hidden_task_count = await self.settings_service.get("hidden_task_count", 60)
            tool_budget = await self.settings_service.get("tool_call_budget_per_task", 40)
            score_aggregation_mode = await self.settings_service.get("score_aggregation", "best")

            # 3. Batched Data Retrieval (Zero N+1)
            # Fetch all assignments for this submission
            assign_stmt = (
                sa.select(TaskAssignment)
                .where(TaskAssignment.submission_id == submission_id)
                .order_by(TaskAssignment.assigned_at.asc())
            )
            assignments = list((await self.session.execute(assign_stmt)).scalars().all())
            assign_by_task = {a.task_id: a for a in assignments}
            assigned_task_ids = [a.task_id for a in assignments]

            # Fetch tasks matching assigned_task_ids
            tasks_by_id: dict[str, Task] = {}
            if assigned_task_ids:
                assigned_tasks = list(
                    (await self.session.execute(sa.select(Task).where(Task.task_id.in_(assigned_task_ids))))
                    .scalars()
                    .all()
                )
                for t in assigned_tasks:
                    tasks_by_id[t.task_id] = t

            # Also check if per_task_results has any task_ids not in assigned_task_ids (e.g. historical records)
            sub_results = sub.per_task_results or []
            extra_tids = [
                r["task_id"]
                for r in sub_results
                if isinstance(r, dict) and "task_id" in r and r["task_id"] not in tasks_by_id
            ]
            if extra_tids:
                extra_tasks = list(
                    (await self.session.execute(sa.select(Task).where(Task.task_id.in_(extra_tids)))).scalars().all()
                )
                for t in extra_tasks:
                    tasks_by_id[t.task_id] = t

            # If total tasks scored is less than hidden_task_count, fetch remaining unstarted hidden tasks to complete the sealed denominator
            remaining_count = hidden_task_count - len(tasks_by_id)
            if remaining_count > 0:
                unstarted_stmt = sa.select(Task).where(Task.dataset == "hidden")
                if tasks_by_id:
                    unstarted_stmt = unstarted_stmt.where(Task.task_id.not_in(list(tasks_by_id.keys())))
                unstarted_stmt = unstarted_stmt.order_by(Task.task_id.asc()).limit(remaining_count)
                unstarted_tasks = list((await self.session.execute(unstarted_stmt)).scalars().all())
                for ut in unstarted_tasks:
                    tasks_by_id[ut.task_id] = ut

            tasks = list(tasks_by_id.values())
            if not tasks:
                raise HTTPException(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    detail={"error": "NO_HIDDEN_TASKS", "message": "No hidden benchmark tasks found in database."},
                )

            # Fetch all tool call logs for this submission
            logs_stmt = (
                sa.select(ToolCallLog)
                .where(ToolCallLog.submission_id == submission_id)
                .order_by(ToolCallLog.created_at.asc())
            )
            logs = list((await self.session.execute(logs_stmt)).scalars().all())
            logs_by_task: dict[str, list[dict[str, Any]]] = {}
            for log in logs:
                if log.task_id:
                    logs_by_task.setdefault(log.task_id, []).append(
                        {
                            "tool_name": log.tool_name,
                            "request_payload": log.request_payload,
                            "response_payload": log.response_payload,
                            "was_enforcement_rejection": log.was_enforcement_rejection,
                            "created_at": log.created_at.isoformat() if log.created_at else None,
                        }
                    )

            # Map per_task_results by task_id
            results_by_task: dict[str, dict[str, Any]] = {}
            for r in sub.per_task_results or []:
                if isinstance(r, dict) and "task_id" in r:
                    results_by_task[r["task_id"]] = r

            # 4. In-Memory Task Evaluation
            scored_results: list[TaskScoreResult] = []
            updated_per_task_records: list[dict[str, Any]] = []

            total_task_aggregates = 0.0
            dim_totals = {
                "task_success": 0.0,
                "policy": 0.0,
                "robustness": 0.0,
                "evidence": 0.0,
                "calibration": 0.0,
                "efficiency": 0.0,
                "communication": 0.0,
            }
            non_normal_robustness_sum = 0.0
            non_normal_tasks_count = 0

            for task in tasks:
                tid = task.task_id
                assign = assign_by_task.get(tid)
                sub_rec = results_by_task.get(tid)
                task_logs = logs_by_task.get(tid, [])

                runtime_state = assign.world_runtime_state if assign else None

                task_result = TaskEvaluator.evaluate_task(
                    task_id=tid,
                    world_seed=task.world_state_seed,
                    ground_truth=task.ground_truth,
                    runtime_state=runtime_state,
                    submission_record=sub_rec,
                    tool_logs=task_logs,
                    tool_call_budget=tool_budget,
                    weights=weights,
                    input_payload=task.input_payload,
                )
                scored_results.append(task_result)

                # Accumulate scores
                scores_dict = task_result.scores.model_dump()
                total_task_aggregates += scores_dict["task_aggregate"]
                for d in dim_totals:
                    dim_totals[d] += scores_dict[d]

                if task.variant != "normal":
                    non_normal_robustness_sum += scores_dict["robustness"]
                    non_normal_tasks_count += 1

                # Update/enrich per_task_results entry
                rec: dict[str, Any] = sub_rec.copy() if sub_rec else {"task_id": tid, "status": "unstarted"}
                rec["scores"] = scores_dict
                rec["audit"] = task_result.audit
                updated_per_task_records.append(rec)

            # 5. Submission-Level Aggregation
            n_tasks = len(tasks)
            submission_aggregate = total_task_aggregates / float(n_tasks) if n_tasks > 0 else 0.0

            avg_dimensions = {}
            for d, total in dim_totals.items():
                if d == "robustness" and non_normal_tasks_count > 0:
                    avg_dimensions[d] = round(non_normal_robustness_sum / float(non_normal_tasks_count), 4)
                else:
                    avg_dimensions[d] = round(total / float(n_tasks), 4)

            score_run_id = str(uuid.uuid4())
            now_utc = datetime.now(UTC)

            breakdown = {
                "dimensions": avg_dimensions,
                "scoring_metadata": {
                    "scoring_engine_version": SCORING_ENGINE_VERSION,
                    "score_run_id": score_run_id,
                    "scored_at": now_utc.isoformat(),
                    "scoring_weights_snapshot": weights,
                    "score_aggregation_mode": score_aggregation_mode,
                    "tasks_total": n_tasks,
                    "tasks_completed": len([r for r in updated_per_task_records if r.get("status") == "completed"]),
                    "tasks_timed_out": len([r for r in updated_per_task_records if r.get("status") == "timed_out"]),
                    "tasks_unstarted": len([r for r in updated_per_task_records if r.get("status") == "unstarted"]),
                },
            }

            # 6. Atomic Persistence in PostgreSQL
            sub.aggregate_score = round(submission_aggregate, 4)
            sub.breakdown = breakdown
            sub.per_task_results = updated_per_task_records
            flag_modified(sub, "per_task_results")
            flag_modified(sub, "breakdown")
            await self.session.commit()

            return SubmissionScoreResult(
                submission_id=str(sub.submission_id),
                team_id=str(sub.team_id),
                attempt_number=sub.attempt_number,
                status=sub.status,
                aggregate_score=float(sub.aggregate_score),
                breakdown=breakdown,
                tasks_scored=scored_results,
            )

    async def get_team_aggregate_score(self, team_id: uuid.UUID) -> TeamScoreSummary:
        """Computes team-level aggregate score across multiple submissions per settings.score_aggregation."""
        aggregation_mode = await self.settings_service.get("score_aggregation", "best")

        stmt = (
            sa.select(Submission)
            .where(
                Submission.team_id == team_id,
                Submission.status.in_(["completed", "expired"]),
                Submission.aggregate_score.is_not(None),
            )
            .order_by(Submission.attempt_number.asc())
        )
        submissions = list((await self.session.execute(stmt)).scalars().all())

        if not submissions:
            return TeamScoreSummary(
                team_id=str(team_id),
                score_aggregation=aggregation_mode,
                team_score=0.0,
                submissions_count=0,
                submission_scores=[],
            )

        submission_scores = [
            {
                "submission_id": str(s.submission_id),
                "attempt_number": s.attempt_number,
                "aggregate_score": float(s.aggregate_score) if s.aggregate_score is not None else 0.0,
                "completed_at": s.completed_at.isoformat() if s.completed_at else None,
            }
            for s in submissions
        ]

        scores: list[float] = [float(s.aggregate_score) if s.aggregate_score is not None else 0.0 for s in submissions]

        if aggregation_mode == "best":
            team_score = max(scores)
        elif aggregation_mode == "last":
            team_score = scores[-1]
        elif aggregation_mode == "average":
            team_score = sum(scores) / float(len(scores))
        else:
            team_score = max(scores)

        return TeamScoreSummary(
            team_id=str(team_id),
            score_aggregation=aggregation_mode,
            team_score=round(team_score, 4),
            submissions_count=len(submissions),
            submission_scores=submission_scores,
        )

    async def score_all_completed_submissions(
        self,
        team_id: uuid.UUID | None = None,
        force: bool = False,
    ) -> list[SubmissionScoreResult]:
        """Batch evaluation job: scores all completed submissions."""
        query = sa.select(Submission.submission_id).where(Submission.status.in_(["completed", "expired"]))
        if team_id:
            query = query.where(Submission.team_id == team_id)
        if not force:
            query = query.where(Submission.aggregate_score.is_(None))

        submission_ids = list((await self.session.execute(query)).scalars().all())
        results = []
        for sid in submission_ids:
            res = await self.score_submission(sid, force=force)
            results.append(res)
        return results
