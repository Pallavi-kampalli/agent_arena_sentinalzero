import pytest

from agent_arena.scoring.evaluator import TaskEvaluator


@pytest.fixture
def mock_task_ground_truth():
    return {
        "expected_resolution": "refund",
        "must_escalate": False,
        "required_evidence": ["TXN-101", "DOC-1001"],
        "expected_action": {
            "tool": "issue_refund",
            "params": {"transaction_id": "TXN-101", "amount": 99.0},
        },
        "expected_end_state": {
            "transactions": [{"id": "TXN-101", "refund_status": "refunded", "refunded_amount": 99.0}]
        },
    }


@pytest.fixture
def mock_world_seed():
    return {
        "transactions": [{"id": "TXN-101", "refund_status": "completed", "refunded_amount": 0.0}],
        "customers": [{"id": "CUS-5001"}],
    }


def test_adversarial_fabricated_evidence_ids(mock_world_seed, mock_task_ground_truth):
    """Participant submits fabricated evidence IDs and IDs from another task/team."""
    # Participant only retrieved DOC-1001 in tool logs.
    tool_logs = [
        {
            "tool_name": "get_document",
            "request_payload": {"document_id": "DOC-1001"},
            "response_payload": {"document": {"id": "DOC-1001", "title": "Refund Policy"}},
        }
    ]
    # Participant claims DOC-1001, but also TXN-101 (which they never retrieved),
    # plus completely fake IDs: DOC-FAKE-999, TXN-OTHER-TEAM-888
    submission_record = {
        "status": "completed",
        "submission_payload": {
            "case_classification": {"category": "billing", "issue": "refund", "severity": "low"},
            "decision": {"resolution": "refund", "escalation_required": False},
            "evidence": ["DOC-1001", "TXN-101", "DOC-FAKE-999", "TXN-OTHER-TEAM-888"],
            "uncertainties": [],
            "customer_response": "Hello, your refund has been processed. Best regards.",
            "confidence": 0.9,
        },
    }
    runtime_state = {"transactions": [{"id": "TXN-101", "refund_status": "refunded", "refunded_amount": 99.0}]}

    result = TaskEvaluator.evaluate_task(
        task_id="TASK-HIDDEN-001",
        world_seed=mock_world_seed,
        ground_truth=mock_task_ground_truth,
        runtime_state=runtime_state,
        submission_record=submission_record,
        tool_logs=tool_logs,
        input_payload={"customer_id": "CUS-5001"},
    )

    # Valid cited evidence is only DOC-1001 (TP = 1).
    # TXN-101 was not observed -> rejected from TP.
    # Total cited = 4. Precision = 1 / 4 = 0.25.
    # Total required = 2. Recall = 1 / 2 = 0.50.
    # F1 = (2 * 0.25 * 0.50) / (0.25 + 0.50) = 0.25 / 0.75 = 0.3333.
    audit = result.audit
    assert audit["precision"] == 0.25
    assert audit["recall"] == 0.5
    assert audit["f1"] == pytest.approx(0.3333, abs=1e-3)
    assert result.scores.evidence == pytest.approx(0.3333, abs=1e-3)


def test_adversarial_sealed_timeout_is_zero(mock_world_seed, mock_task_ground_truth):
    """Task marked timed_out must yield 0.0 across all dimensions."""
    submission_record = {
        "status": "timed_out",
        "assigned_at": "2026-09-15T08:00:00Z",
        "timed_out_at": "2026-09-15T08:03:01Z",
    }
    # Even if runtime state had some mutations or logs exist, timed_out task is sealed at 0.0
    runtime_state = {"transactions": [{"id": "TXN-101", "refund_status": "refunded", "refunded_amount": 99.0}]}
    tool_logs = [{"tool_name": "issue_refund", "was_enforcement_rejection": False, "response_payload": {}}]

    result = TaskEvaluator.evaluate_task(
        task_id="TASK-HIDDEN-001",
        world_seed=mock_world_seed,
        ground_truth=mock_task_ground_truth,
        runtime_state=runtime_state,
        submission_record=submission_record,
        tool_logs=tool_logs,
        input_payload={"customer_id": "CUS-5001"},
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


def test_adversarial_unstarted_task_is_zero(mock_world_seed, mock_task_ground_truth):
    """Task never started must yield 0.0 across all dimensions."""
    result = TaskEvaluator.evaluate_task(
        task_id="TASK-HIDDEN-002",
        world_seed=mock_world_seed,
        ground_truth=mock_task_ground_truth,
        runtime_state=None,
        submission_record=None,
        tool_logs=[],
        input_payload={"customer_id": "CUS-5001"},
    )

    assert result.status == "unstarted"
    assert result.scores.task_success == 0.0
    assert result.scores.task_aggregate == 0.0


def test_adversarial_lying_about_resolution(mock_world_seed):
    """Participant submits resolution='refund' when chargeback hold blocked the refund."""
    ground_truth = {
        "expected_resolution": "escalate",
        "must_escalate": True,
        "required_evidence": ["TXN-101", "DOC-1842"],
        "expected_action": {
            "tool": "escalate_case",
            "params": {"case_id": "CASE-101", "team": "billing_specialists"},
        },
        "expected_end_state": {
            "transactions": [{"id": "TXN-101", "refund_status": "completed", "refunded_amount": 0.0}],
            "escalations": [{"case_id": "CASE-101"}],
        },
    }
    # Tool log shows the refund was rejected by server-side enforcement
    tool_logs = [
        {
            "tool_name": "issue_refund",
            "request_payload": {"transaction_id": "TXN-101", "amount": 99.0},
            "response_payload": {
                "error": "INELIGIBLE",
                "reason": "chargeback_investigation_active",
                "policy_ref": "DOC-1842",
            },
            "was_enforcement_rejection": True,
        }
    ]
    # Participant ignored rejection, did not escalate, and falsely submitted resolution='refund'
    submission_record = {
        "status": "completed",
        "submission_payload": {
            "case_classification": {"category": "billing", "issue": "refund", "severity": "high"},
            "decision": {"resolution": "refund", "escalation_required": False},
            "evidence": ["TXN-101"],
            "uncertainties": [],
            "customer_response": "We have processed your refund.",
            "confidence": 0.99,  # Extremely confident in their wrong answer
        },
    }
    runtime_state = {
        "transactions": [{"id": "TXN-101", "refund_status": "completed", "refunded_amount": 0.0}],
        "escalations": [],
    }

    result = TaskEvaluator.evaluate_task(
        task_id="TASK-HIDDEN-003",
        world_seed=mock_world_seed,
        ground_truth=ground_truth,
        runtime_state=runtime_state,
        submission_record=submission_record,
        tool_logs=tool_logs,
        input_payload={"customer_id": "CUS-5001"},
    )

    # 1. Task Success: 0.0 (escalation did not occur, refund did not occur)
    assert result.scores.task_success == 0.0
    # 2. Policy: 0.0 (claimed refund when no refund occurred -> truthfulness = 0.0)
    assert result.scores.policy == 0.0
    # 3. Robustness: 0.0
    assert result.scores.robustness == 0.0
    # 4. Calibration: 0.0 (must_escalate is True, but escalation_required is False)
    assert result.scores.calibration == 0.0
    # 5. Communication: heavily penalized (unsupported promise to customer)
    assert result.scores.communication <= 0.50
    # Overall task aggregate should be extremely low
    assert result.scores.task_aggregate < 0.15


def test_adversarial_boundary_confidence_values(mock_world_seed, mock_task_ground_truth):
    """Evaluates boundary confidence values (0.0 and 1.0) and verifies safe numeric clamping."""
    runtime_state = {"transactions": [{"id": "TXN-101", "refund_status": "refunded", "refunded_amount": 99.0}]}
    tool_logs = [
        {
            "tool_name": "issue_refund",
            "request_payload": {"transaction_id": "TXN-101", "amount": 99.0},
            "response_payload": {"status": "refunded", "transaction": {"id": "TXN-101"}},
            "was_enforcement_rejection": False,
        },
        {
            "tool_name": "get_document",
            "request_payload": {"document_id": "DOC-1001"},
            "response_payload": {"document": {"id": "DOC-1001"}},
            "was_enforcement_rejection": False,
        },
    ]

    # Test confidence = 0.0 on correct answer
    sub_conf_0 = {
        "status": "completed",
        "submission_payload": {
            "case_classification": {"category": "billing", "issue": "refund", "severity": "low"},
            "decision": {"resolution": "refund", "escalation_required": False},
            "evidence": ["TXN-101", "DOC-1001"],
            "uncertainties": [],
            "customer_response": "Hello, your refund has been processed. Best regards.",
            "confidence": 0.0,
        },
    }
    res_0 = TaskEvaluator.evaluate_task(
        task_id="TASK-HIDDEN-001",
        world_seed=mock_world_seed,
        ground_truth=mock_task_ground_truth,
        runtime_state=runtime_state,
        submission_record=sub_conf_0,
        tool_logs=tool_logs,
        input_payload={"customer_id": "CUS-5001"},
    )
    # Since confidence is 0.0, calibration on correct task is 0.0
    assert res_0.scores.calibration == 0.0

    # Test confidence = 1.0 on correct answer
    sub_conf_1 = {
        "status": "completed",
        "submission_payload": {
            "case_classification": {"category": "billing", "issue": "refund", "severity": "low"},
            "decision": {"resolution": "refund", "escalation_required": False},
            "evidence": ["TXN-101", "DOC-1001"],
            "uncertainties": [],
            "customer_response": "Hello, your refund has been processed. Best regards.",
            "confidence": 1.0,
        },
    }
    res_1 = TaskEvaluator.evaluate_task(
        task_id="TASK-HIDDEN-001",
        world_seed=mock_world_seed,
        ground_truth=mock_task_ground_truth,
        runtime_state=runtime_state,
        submission_record=sub_conf_1,
        tool_logs=tool_logs,
        input_payload={"customer_id": "CUS-5001"},
    )
    assert res_1.scores.calibration == 1.0
