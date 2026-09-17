"""Adversarial scoring tests for the SentinelZero evaluation engine.

Tests that the scoring engine correctly handles:
- Fabricated evidence IDs (never retrieved via tools)
- Wrong resolution with high confidence
- Boundary confidence values (0.0, 1.0)
- Timed-out and unstarted task sealing
"""

import pytest

from agent_arena.scoring.evaluator import TaskEvaluator


@pytest.fixture
def sz_ground_truth():
    """SentinelZero ground truth: quarantine a suspicious phishing email."""
    return {
        "expected_resolution": "quarantine",
        "must_escalate": False,
        "required_evidence": ["MSG-HIDDEN-001", "DOM-MALICIOUS-001"],
        "expected_action_tool": "quarantine_message",
    }


@pytest.fixture
def sz_world_seed():
    """Minimal SentinelZero world seed with a target message and threat intel."""
    return {
        "target_message_id": "MSG-HIDDEN-001",
        "directory": [],
        "domains": [],
        "threat_intel": [{"domain_id": "DOM-MALICIOUS-001", "domain": "evil-corp.example", "reputation": "malicious"}],
        "security_policies": [],
        "threads": [],
        "actions_taken": [],
    }


def test_adversarial_fabricated_evidence_ids(sz_world_seed, sz_ground_truth):
    """Participant submits fabricated evidence IDs never retrieved via tools.

    Only MSG-HIDDEN-001 was retrieved (via tool response). DOM-MALICIOUS-001 was NOT
    retrieved. Participant fabricates DOM-FAKE-999 and POL-OTHER-888 too.
    Valid TP = 1 (MSG-HIDDEN-001 only). Precision = 1/4. Recall = 1/2. F1 ≈ 0.3333.
    """
    tool_logs = [
        {
            "tool_name": "get_email_headers",
            "request_payload": {"message_id": "MSG-HIDDEN-001"},
            "response_payload": {"message_id": "MSG-HIDDEN-001", "auth_results": {"spf": "fail"}},
            "was_enforcement_rejection": False,
        }
    ]
    submission_record = {
        "status": "completed",
        "submission_payload": {
            "decision": {"resolution": "quarantine", "escalation_required": False},
            "evidence": ["MSG-HIDDEN-001", "DOM-MALICIOUS-001", "DOM-FAKE-999", "POL-OTHER-888"],
            "customer_response": "Quarantined the suspicious email.",
            "confidence": 0.9,
        },
    }
    runtime_state = dict(sz_world_seed)

    result = TaskEvaluator.evaluate_task(
        task_id="TASK-HIDDEN-001",
        world_seed=sz_world_seed,
        ground_truth=sz_ground_truth,
        runtime_state=runtime_state,
        submission_record=submission_record,
        tool_logs=tool_logs,
        input_payload={"message_id": "MSG-HIDDEN-001"},
    )

    # Fabricated IDs (DOM-MALICIOUS-001) not in tool logs → rejected as TP
    # Valid TP = 1 (MSG-HIDDEN-001). Submitted = 4. Required = 2.
    # Precision = 1/4 = 0.25; Recall = 1/2 = 0.50; F1 ≈ 0.3333
    audit = result.audit
    assert audit["precision"] == pytest.approx(0.25, abs=1e-3)
    assert audit["recall"] == pytest.approx(0.50, abs=1e-3)
    assert audit["f1"] == pytest.approx(0.3333, abs=1e-3)
    assert result.scores.evidence == pytest.approx(0.3333, abs=1e-3)


def test_adversarial_sealed_timeout_is_zero(sz_world_seed, sz_ground_truth):
    """Task marked timed_out must yield 0.0 across all dimensions."""
    submission_record = {
        "status": "timed_out",
        "assigned_at": "2026-09-15T08:00:00Z",
        "timed_out_at": "2026-09-15T08:03:01Z",
    }
    runtime_state = dict(sz_world_seed)
    tool_logs = [{"tool_name": "lookup_directory", "was_enforcement_rejection": False, "response_payload": {}}]

    result = TaskEvaluator.evaluate_task(
        task_id="TASK-HIDDEN-001",
        world_seed=sz_world_seed,
        ground_truth=sz_ground_truth,
        runtime_state=runtime_state,
        submission_record=submission_record,
        tool_logs=tool_logs,
        input_payload={"message_id": "MSG-HIDDEN-001"},
    )

    assert result.status == "timed_out"
    assert result.scores.task_success == 0.0
    assert result.scores.policy == 0.0
    assert result.scores.robustness == 0.0
    assert result.scores.evidence == 0.0
    assert result.scores.calibration == 0.0
    assert result.scores.efficiency == 0.0
    assert result.scores.communication == 0.0
    assert result.scores.task_aggregate == 0.0


def test_adversarial_unstarted_task_is_zero(sz_world_seed, sz_ground_truth):
    """Task never started must yield 0.0 across all dimensions."""
    result = TaskEvaluator.evaluate_task(
        task_id="TASK-HIDDEN-002",
        world_seed=sz_world_seed,
        ground_truth=sz_ground_truth,
        runtime_state=None,
        submission_record=None,
        tool_logs=[],
        input_payload={"message_id": "MSG-HIDDEN-002"},
    )

    assert result.status == "unstarted"
    assert result.scores.task_success == 0.0
    assert result.scores.task_aggregate == 0.0


def test_adversarial_lying_about_resolution(sz_world_seed):
    """Participant submits ALLOW when ground truth is ESCALATE (safety-critical miss → 0.0).

    The participant:
    - Called quarantine_message which was rejected (enforcement rejection logged)
    - Still submitted resolution='allow' instead of escalating
    - Uses high confidence 0.99 (should not help calibration)
    """
    ground_truth = {
        "expected_resolution": "escalate",
        "must_escalate": True,
        "required_evidence": ["MSG-HIDDEN-003", "DOM-APT-001"],
        "expected_action_tool": "escalate_to_tier2_soc",
    }
    tool_logs = [
        {
            "tool_name": "quarantine_message",
            "request_payload": {"message_id": "MSG-HIDDEN-003", "reason": "suspected APT"},
            "response_payload": {"status": "warning_applied"},
            "was_enforcement_rejection": True,
        }
    ]
    submission_record = {
        "status": "completed",
        "submission_payload": {
            "decision": {"resolution": "allow", "escalation_required": False},
            "evidence": ["MSG-HIDDEN-003"],
            "customer_response": "The email looks fine. Delivered.",
            "confidence": 0.99,
        },
    }
    runtime_state = dict(sz_world_seed)

    result = TaskEvaluator.evaluate_task(
        task_id="TASK-HIDDEN-003",
        world_seed=sz_world_seed,
        ground_truth=ground_truth,
        runtime_state=runtime_state,
        submission_record=submission_record,
        tool_logs=tool_logs,
        input_payload={"message_id": "MSG-HIDDEN-003"},
    )

    # ALLOW when ground truth is ESCALATE → safety-critical miss
    assert result.scores.task_success == 0.0
    # Policy: enforcement rejection but still submitted ALLOW (penalized)
    assert result.scores.policy == 0.0
    # Robustness: 0.0 (based on task_success)
    assert result.scores.robustness == 0.0
    # Calibration: must_escalate=True but escalation_required=False → 0.0
    assert result.scores.calibration == 0.0
    # Overall aggregate is bounded by the weight of efficiency + evidence + communication only
    # (task_success=0, policy=0, robustness=0, calibration=0 — worst-case total < 0.35)
    assert result.scores.task_aggregate < 0.35


def test_adversarial_boundary_confidence_values(sz_world_seed, sz_ground_truth):
    """Evaluates boundary confidence values (0.0 and 1.0) with correct QUARANTINE decision."""
    # Perfect quarantine tool log with observed evidence
    tool_logs = [
        {
            "tool_name": "get_email_headers",
            "request_payload": {"message_id": "MSG-HIDDEN-001"},
            "response_payload": {"message_id": "MSG-HIDDEN-001", "auth_results": {"spf": "fail", "dkim": "fail"}},
            "was_enforcement_rejection": False,
        },
        {
            "tool_name": "inspect_domain_reputation",
            "request_payload": {"domain": "evil-corp.example"},
            "response_payload": {"domain_id": "DOM-MALICIOUS-001", "reputation": "malicious"},
            "was_enforcement_rejection": False,
        },
    ]
    runtime_state = dict(sz_world_seed)

    # confidence = 0.0 on correct answer:
    # task_success < 1.0 (decision correct but action not executed) → c_align = 1.0 - 0.0 = 1.0
    # With must_escalate=False and escalation_required=False, m_escalate = 1.0
    # So calibration = 1.0 * 1.0 = 1.0 (paradoxically correct — low conf on imperfect submission)
    sub_conf_0 = {
        "status": "completed",
        "submission_payload": {
            "decision": {"resolution": "quarantine", "escalation_required": False},
            "evidence": ["MSG-HIDDEN-001", "DOM-MALICIOUS-001"],
            "customer_response": "Email quarantined due to phishing indicators.",
            "confidence": 0.0,
        },
    }
    res_0 = TaskEvaluator.evaluate_task(
        task_id="TASK-HIDDEN-001",
        world_seed=sz_world_seed,
        ground_truth=sz_ground_truth,
        runtime_state=runtime_state,
        submission_record=sub_conf_0,
        tool_logs=tool_logs,
        input_payload={"message_id": "MSG-HIDDEN-001"},
    )
    # confidence = 0.0 on answer where decision correct but action missing (task_success=0.55):
    # c_align = 1.0 - conf = 1.0 → calibration = 1.0
    # This represents: "not confident on an imperfect submission" → well calibrated
    assert res_0.scores.calibration == pytest.approx(1.0, abs=0.01)

    # confidence = 1.0 on correct answer → calibration should be 1.0
    sub_conf_1 = {
        "status": "completed",
        "submission_payload": {
            "decision": {"resolution": "quarantine", "escalation_required": False},
            "evidence": ["MSG-HIDDEN-001", "DOM-MALICIOUS-001"],
            "customer_response": "Email quarantined due to phishing indicators.",
            "confidence": 1.0,
        },
    }
    res_1 = TaskEvaluator.evaluate_task(
        task_id="TASK-HIDDEN-001",
        world_seed=sz_world_seed,
        ground_truth=sz_ground_truth,
        runtime_state=runtime_state,
        submission_record=sub_conf_1,
        tool_logs=tool_logs,
        input_payload={"message_id": "MSG-HIDDEN-001"},
    )
    # confidence=1.0 on task_success=0.55 (correct decision, wrong/no action):
    # c_align = 1.0 - 1.0 = 0.0 → calibration = 0.0 (overconfident on imperfect answer)
    assert res_1.scores.calibration == pytest.approx(0.0, abs=0.01)
