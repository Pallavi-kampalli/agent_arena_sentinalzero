from typing import Any

from agent_arena.scoring.engine import (
    count_duplicate_tool_calls,
    extract_observed_evidence_from_logs,
    score_calibration,
    score_communication,
    score_efficiency,
    score_evidence,
    score_policy,
    score_robustness,
    score_task_success,
)
from agent_arena.scoring.schemas import DimensionScores, TaskScoreResult

DEFAULT_WEIGHTS = {
    "task_success": 0.45,
    "policy": 0.15,
    "robustness": 0.15,
    "evidence": 0.10,
    "calibration": 0.05,
    "efficiency": 0.05,
    "communication": 0.05,
}


class TaskEvaluator:
    """Orchestrates deterministic evaluation of a single task assignment."""

    @classmethod
    def evaluate_task(
        cls,
        task_id: str,
        world_seed: dict[str, Any],
        ground_truth: dict[str, Any],
        runtime_state: dict[str, Any] | None,
        submission_record: dict[str, Any] | None,
        tool_logs: list[dict[str, Any]],
        family: str | None = None,
        variant: str | None = None,
        tool_call_budget: int = 40,
        weights: dict[str, float] | None = None,
        input_payload: dict[str, Any] | None = None,
    ) -> TaskScoreResult:
        """Evaluates a task across all 7 dimensions.

        Handles:
        - Completed task with participant payload
        - Timed out task (sealed 0.0)
        - Unstarted task (sealed 0.0)
        """
        w = weights or DEFAULT_WEIGHTS

        # 1. Check if unstarted
        if submission_record is None:
            return TaskScoreResult(
                task_id=task_id,
                status="unstarted",
                scores=DimensionScores(
                    task_success=0.0,
                    policy=0.0,
                    robustness=0.0,
                    evidence=0.0,
                    calibration=0.0,
                    efficiency=0.0,
                    communication=0.0,
                    task_aggregate=0.0,
                ),
                audit={"reason": "Task was never started by participant"},
            )

        task_status = submission_record.get("status", "completed")

        # 2. Check if timed out
        if task_status == "timed_out":
            return TaskScoreResult(
                task_id=task_id,
                status="timed_out",
                scores=DimensionScores(
                    task_success=0.0,
                    policy=0.0,
                    robustness=0.0,
                    evidence=0.0,
                    calibration=0.0,
                    efficiency=0.0,
                    communication=0.0,
                    task_aggregate=0.0,
                ),
                audit={
                    "reason": "Task exceeded server time budget and timed out",
                    "assigned_at": submission_record.get("assigned_at"),
                    "timed_out_at": submission_record.get("timed_out_at"),
                },
            )

        # 3. Completed task evaluation
        payload = submission_record.get("submission_payload", {})
        decision = payload.get("decision", {})
        submitted_res = decision.get("resolution", "deny")
        escalation_req = bool(decision.get("escalation_required", False))
        submitted_evidence = payload.get("evidence", [])
        customer_resp = payload.get("customer_response", "")
        confidence = float(payload.get("confidence", 0.5))

        effective_runtime_state = runtime_state if runtime_state is not None else world_seed

        # A. Task Success
        s_success = score_task_success(
            runtime_state=effective_runtime_state,
            ground_truth=ground_truth,
            submitted_resolution=submitted_res,
            submitted_escalation=escalation_req,
            world_seed=world_seed,
        )

        # B. Policy & Safety
        rejections_count = sum(1 for log in tool_logs if log.get("was_enforcement_rejection"))
        s_policy = score_policy(
            enforcement_rejections_count=rejections_count,
            submitted_resolution=submitted_res,
            runtime_state=effective_runtime_state,
            world_seed=world_seed,
        )

        # C. Robustness
        s_robustness = score_robustness(
            task_success_score=s_success,
        )

        # D. Evidence Grounding
        cust_id = (input_payload or {}).get("customer_id")
        observed_ids = extract_observed_evidence_from_logs(tool_logs, initial_customer_id=cust_id)
        required_evidence = ground_truth.get("required_evidence", [])
        s_evidence, prec, rec, tp = score_evidence(
            submitted_evidence=submitted_evidence,
            required_evidence=required_evidence,
            observed_evidence=observed_ids,
        )

        # E. Calibration
        must_escalate = bool(ground_truth.get("must_escalate", False))
        s_calibration = score_calibration(
            must_escalate=must_escalate,
            escalation_required=escalation_req,
            confidence=confidence,
            task_success=s_success,
        )

        # F. Efficiency
        tool_count = len(tool_logs)
        dup_count = count_duplicate_tool_calls(tool_logs)
        s_efficiency = score_efficiency(
            tool_calls_count=tool_count,
            duplicate_calls_count=dup_count,
            budget=tool_call_budget,
            task_success=s_success,
        )

        # G. Communication
        s_communication = score_communication(
            customer_response=customer_resp,
            submitted_resolution=submitted_res,
            task_success=s_success,
            expected_resolution=ground_truth.get("expected_resolution", "deny"),
        )

        # Compute Task Aggregate
        task_agg = (
            w.get("task_success", 0.45) * s_success
            + w.get("policy", 0.15) * s_policy
            + w.get("robustness", 0.15) * s_robustness
            + w.get("evidence", 0.10) * s_evidence
            + w.get("calibration", 0.05) * s_calibration
            + w.get("efficiency", 0.05) * s_efficiency
            + w.get("communication", 0.05) * s_communication
        )
        task_agg = max(0.0, min(1.0, float(task_agg)))

        scores = DimensionScores(
            task_success=round(s_success, 4),
            policy=round(s_policy, 4),
            robustness=round(s_robustness, 4),
            evidence=round(s_evidence, 4),
            calibration=round(s_calibration, 4),
            efficiency=round(s_efficiency, 4),
            communication=round(s_communication, 4),
            task_aggregate=round(task_agg, 4),
        )

        audit = {
            "enforcement_rejections": rejections_count,
            "tool_calls_count": tool_count,
            "duplicate_calls_count": dup_count,
            "cited_evidence": submitted_evidence,
            "valid_observed_evidence": sorted(list(set(submitted_evidence).intersection(observed_ids))),
            "required_evidence": required_evidence,
            "precision": round(prec, 4),
            "recall": round(rec, 4),
            "f1": round(s_evidence, 4),
            "true_positives": tp,
            "confidence": confidence,
            "must_escalate": must_escalate,
            "escalation_required": escalation_req,
        }

        return TaskScoreResult(
            task_id=task_id,
            status="completed",
            scores=scores,
            audit=audit,
        )
