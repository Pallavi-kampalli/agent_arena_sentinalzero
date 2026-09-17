"""SentinelZero Task Evaluator.

Replaces the generic SupportOps TaskEvaluator with SentinelZero-specific
scoring for cybersecurity triage decisions (ALLOW, WARN, QUARANTINE, ESCALATE).

SentinelZero Scoring Weights:
  task_success:  35%
  policy:        15%
  evidence:      15%
  calibration:   10%
  efficiency:    10%
  communication: 10%
  robustness:     5%
  ─────────────────
  TOTAL:        100%
"""

from typing import Any

from agent_arena.scoring.schemas import DimensionScores, TaskScoreResult
from agent_arena.scoring.sentinelzero_engine import (
    DECISION_TO_ACTION,
    VALID_DECISIONS,
    sz_extract_observed_evidence,
    sz_score_calibration,
    sz_score_communication,
    sz_score_efficiency,
    sz_score_evidence,
    sz_score_policy,
    sz_score_robustness,
    sz_score_task_success,
)

# ─────────────────────────────────────────────────────────────────────────────
# SentinelZero Scoring Weights per PRD §5
# ─────────────────────────────────────────────────────────────────────────────
SENTINELZERO_WEIGHTS: dict[str, float] = {
    "task_success": 0.35,
    "policy": 0.15,
    "evidence": 0.15,
    "calibration": 0.10,
    "efficiency": 0.10,
    "communication": 0.10,
    "robustness": 0.05,
}

# Tool call budget per task (how many tools is it reasonable to call?)
# 5 read tools + 1 action tool = 6 minimum required; budget allows some flexibility
SENTINELZERO_TOOL_BUDGET = 10

# ─────────────────────────────────────────────────────────────────────────────
# Duplicate tool call counter (identical to generic engine)
# ─────────────────────────────────────────────────────────────────────────────
import hashlib
import json


def _count_duplicate_tool_calls(tool_logs: list[dict[str, Any]]) -> int:
    """Counts repeated identical tool calls (same tool_name + request_payload)."""
    seen: set[str] = set()
    duplicates = 0
    for log in tool_logs:
        tool_name = log.get("tool_name", "")
        payload = log.get("request_payload") or {}
        key_str = f"{tool_name}:{json.dumps(payload, sort_keys=True)}"
        call_hash = hashlib.sha256(key_str.encode("utf-8")).hexdigest()
        if call_hash in seen:
            duplicates += 1
        else:
            seen.add(call_hash)
    return duplicates


def _extract_action_calls(tool_logs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Returns only the action-tool log entries (not read tool calls)."""
    action_tools = {
        "allow_and_deliver",
        "apply_warning_banner",
        "quarantine_message",
        "escalate_to_tier2_soc",
    }
    return [log for log in tool_logs if log.get("tool_name") in action_tools]


def _infer_action_from_logs(tool_logs: list[dict[str, Any]]) -> tuple[str | None, str | None]:
    """Infers (action_tool_name, message_id) from action tool calls in logs."""
    action_calls = _extract_action_calls(tool_logs)
    if not action_calls:
        return None, None
    # Use the last action call if there are multiple (shouldn't happen in well-formed agent)
    last = action_calls[-1]
    tool_name = last.get("tool_name")
    req = last.get("request_payload") or {}
    msg_id = req.get("message_id")
    return tool_name, msg_id


# ─────────────────────────────────────────────────────────────────────────────
# SentinelZero Task Evaluator
# ─────────────────────────────────────────────────────────────────────────────

class SentinelZeroTaskEvaluator:
    """Orchestrates deterministic SentinelZero evaluation of a single task assignment."""

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
        tool_call_budget: int = SENTINELZERO_TOOL_BUDGET,
        weights: dict[str, float] | None = None,
        input_payload: dict[str, Any] | None = None,
    ) -> TaskScoreResult:
        """Evaluates a SentinelZero task across all 7 dimensions.

        Handles:
        - Completed task with participant payload
        - Timed-out task (sealed 0.0)
        - Unstarted task (sealed 0.0)
        """
        w = weights or SENTINELZERO_WEIGHTS

        # ── 1. Unstarted ──────────────────────────────────────────────────────
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

        # ── 2. Timed out ──────────────────────────────────────────────────────
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

        # ── 3. Completed task evaluation ──────────────────────────────────────
        payload = submission_record.get("submission_payload", {})
        decision = payload.get("decision", {})
        submitted_res = (decision.get("resolution") or "").strip().lower()
        escalation_req = bool(decision.get("escalation_required", False))
        submitted_evidence = payload.get("evidence", []) or []
        summary = payload.get("customer_response") or payload.get("summary") or ""
        confidence_raw = payload.get("confidence", 0.5)

        # Ground truth fields (supports both flat schema and nested ground_truth object)
        gt = ground_truth or {}
        gt_inner = gt.get("ground_truth") if isinstance(gt.get("ground_truth"), dict) else gt

        raw_verdict = gt_inner.get("verdict") or gt.get("expected_resolution") or ""
        expected_res = str(raw_verdict).strip().lower()

        must_escalate = bool(gt_inner.get("must_escalate") if "must_escalate" in gt_inner else gt.get("must_escalate", False))
        required_evidence = gt_inner.get("required_evidence") or gt.get("required_evidence") or []

        expected_action = gt.get("expected_action") or {}
        if not expected_action and gt_inner.get("recommended_action"):
            expected_action = {"tool": gt_inner.get("recommended_action")}
        expected_action_tool = expected_action.get("tool", "") if isinstance(expected_action, dict) else ""
        expected_msg_id = (expected_action.get("params") or {}).get("message_id") if isinstance(expected_action, dict) else None

        # Input message_id (used for evidence seeding and targeting)
        input_msg_id = None
        if input_payload:
            input_msg_id = input_payload.get("message_id") or input_payload.get("customer_message")

        # ── A. Validate confidence ────────────────────────────────────────────
        confidence_valid = (
            confidence_raw is not None
            and isinstance(confidence_raw, (int, float))
            and 0.0 <= float(confidence_raw) <= 1.0
        )
        confidence = float(confidence_raw) if confidence_valid else 0.5
        invalid_confidence = not confidence_valid

        # ── B. Validate decision ──────────────────────────────────────────────
        decision_valid = submitted_res in VALID_DECISIONS
        invalid_decision = not decision_valid

        # ── C. Infer submitted action from tool logs ──────────────────────────
        submitted_action_tool, submitted_msg_id = _infer_action_from_logs(tool_logs)
        action_calls = _extract_action_calls(tool_logs)
        multiple_actions = len(action_calls)

        # Verify action matches decision
        expected_action_for_decision = DECISION_TO_ACTION.get(submitted_res, "")
        action_matches_decision = (submitted_action_tool == expected_action_for_decision)

        # ── D. Task Success ───────────────────────────────────────────────────
        s_success = sz_score_task_success(
            submitted_resolution=submitted_res,
            expected_resolution=expected_res,
            submitted_action_tool=submitted_action_tool,
            expected_action_tool=expected_action_tool,
            submitted_message_id=submitted_msg_id,
            expected_message_id=expected_msg_id,
        )

        # ── E. Policy ─────────────────────────────────────────────────────────
        rejections_count = sum(1 for log in tool_logs if log.get("was_enforcement_rejection"))
        s_policy = sz_score_policy(
            enforcement_rejections_count=rejections_count,
            submitted_resolution=submitted_res,
            expected_resolution=expected_res,
            action_matches_decision=action_matches_decision,
        )

        # ── F. Evidence ───────────────────────────────────────────────────────
        observed_ids = sz_extract_observed_evidence(tool_logs, initial_message_id=input_msg_id)
        s_evidence, prec, rec, tp = sz_score_evidence(
            submitted_evidence=submitted_evidence,
            required_evidence=required_evidence,
            observed_evidence=observed_ids,
        )
        fabricated_count = len([e for e in submitted_evidence if e not in observed_ids])

        # ── G. Calibration ────────────────────────────────────────────────────
        s_calibration = sz_score_calibration(
            confidence=confidence if confidence_valid else None,
            task_success=s_success,
            must_escalate=must_escalate,
            escalation_required=escalation_req,
        )

        # ── H. Efficiency ─────────────────────────────────────────────────────
        tool_count = len(tool_logs)
        dup_count = _count_duplicate_tool_calls(tool_logs)
        s_efficiency = sz_score_efficiency(
            tool_calls_count=tool_count,
            duplicate_calls_count=dup_count,
            budget=tool_call_budget,
            task_success=s_success,
        )

        # ── I. Communication ──────────────────────────────────────────────────
        s_communication = sz_score_communication(
            summary=summary,
            submitted_resolution=submitted_res,
            expected_resolution=expected_res,
            task_success=s_success,
        )

        # ── J. Robustness ─────────────────────────────────────────────────────
        s_robustness = sz_score_robustness(
            task_success=s_success,
            crashed=False,
            invalid_confidence=invalid_confidence,
            invalid_decision=invalid_decision,
            fabricated_evidence_count=fabricated_count,
            multiple_actions_taken=multiple_actions,
        )

        # ── K. Weighted Aggregate ─────────────────────────────────────────────
        task_agg = (
            w.get("task_success", 0.35) * s_success
            + w.get("policy", 0.15) * s_policy
            + w.get("evidence", 0.15) * s_evidence
            + w.get("calibration", 0.10) * s_calibration
            + w.get("efficiency", 0.10) * s_efficiency
            + w.get("communication", 0.10) * s_communication
            + w.get("robustness", 0.05) * s_robustness
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
            "submitted_resolution": submitted_res,
            "expected_resolution": expected_res,
            "submitted_action_tool": submitted_action_tool,
            "expected_action_tool": expected_action_tool,
            "action_matches_decision": action_matches_decision,
            "multiple_actions_taken": multiple_actions,
            "enforcement_rejections": rejections_count,
            "tool_calls_count": tool_count,
            "duplicate_calls_count": dup_count,
            "cited_evidence": submitted_evidence,
            "valid_observed_evidence": sorted(list(set(submitted_evidence).intersection(observed_ids))),
            "fabricated_evidence_count": fabricated_count,
            "required_evidence": required_evidence,
            "precision": round(prec, 4),
            "recall": round(rec, 4),
            "f1": round(s_evidence, 4),
            "true_positives": tp,
            "confidence": confidence,
            "confidence_valid": confidence_valid,
            "must_escalate": must_escalate,
            "escalation_required": escalation_req,
            "invalid_decision": invalid_decision,
        }

        return TaskScoreResult(
            task_id=task_id,
            status="completed",
            scores=scores,
            audit=audit,
        )
