import pytest

from agent_arena.scoring.evaluator import TaskEvaluator
from agent_arena.tasks.generator import FAMILIES, VARIANTS, TaskGenerator
from agent_arena.world.generator import generate_world


@pytest.mark.parametrize("family", FAMILIES)
def test_golden_family_reference_solver_agent_scores_high(family: str):
    """Verifies that an agent providing correct domain action and evidence scores high across all 6 families."""
    world = generate_world(seed=4000)
    variant = "normal"
    task_gen = TaskGenerator(seed=4000)
    task = task_gen.generate_task(world, family, variant, task_id=f"TASK-GOLDEN-{family}")

    gt_dict = task["ground_truth"]
    runtime_state = gt_dict["expected_end_state"]
    required_evidence = gt_dict["required_evidence"]

    # Construct tool logs containing required evidence
    tool_logs = []
    for evid in required_evidence:
        tool_logs.append(
            {
                "tool_name": "get_document" if evid.startswith("DOC") else "get_transactions",
                "request_payload": {"id": evid},
                "response_payload": {"id": evid, "data": "valid"},
                "was_enforcement_rejection": False,
            }
        )

    # Legitimate participant response aligned with ground truth
    sub_payload = {
        "case_classification": task.get("ground_truth", {}).get(
            "classification", {"category": "general", "issue": family, "severity": "medium"}
        ),
        "decision": {
            "resolution": gt_dict["expected_resolution"],
            "escalation_required": gt_dict["must_escalate"],
        },
        "evidence": required_evidence,
        "uncertainties": [],
        "customer_response": f"Hello, thank you for reaching out. We have investigated and resolved your {family} case according to policy. Best regards.",
        "confidence": 0.95,
    }

    res = TaskEvaluator.evaluate_task(
        task_id=task["task_id"],
        family=family,
        variant=variant,
        world_seed=task["world_state_seed"],
        ground_truth=gt_dict,
        runtime_state=runtime_state,
        submission_record={"status": "completed", "submission_payload": sub_payload},
        tool_logs=tool_logs,
        input_payload=task["input_payload"],
    )

    assert res.scores.task_success == 1.0
    assert res.scores.policy == 1.0
    assert res.scores.evidence == 1.0
    assert res.scores.calibration == 0.95
    assert res.scores.task_aggregate >= 0.90


@pytest.mark.parametrize("variant", VARIANTS)
def test_golden_variants_evaluation(variant: str):
    """Verifies that evaluation behaves consistently across all 6 variant types."""
    world = generate_world(seed=5000)
    family = "refund_request"
    task_gen = TaskGenerator(seed=5000)
    task = task_gen.generate_task(world, family, variant, task_id=f"TASK-VARIANT-{variant}")

    gt_dict = task["ground_truth"]
    required_evidence = gt_dict["required_evidence"]

    tool_logs = [{"tool_name": "get_document", "response_payload": {"id": e}} for e in required_evidence]
    sub_payload = {
        "case_classification": task.get("ground_truth", {}).get(
            "classification", {"category": "billing", "issue": "refund", "severity": "medium"}
        ),
        "decision": {
            "resolution": gt_dict["expected_resolution"],
            "escalation_required": gt_dict["must_escalate"],
        },
        "evidence": required_evidence,
        "uncertainties": [],
        "customer_response": "Hello, thank you for reaching out. Your request has been handled according to policy. Regards.",
        "confidence": 0.90,
    }

    res = TaskEvaluator.evaluate_task(
        task_id=task["task_id"],
        family=family,
        variant=variant,
        world_seed=task["world_state_seed"],
        ground_truth=gt_dict,
        runtime_state=gt_dict["expected_end_state"],
        submission_record={"status": "completed", "submission_payload": sub_payload},
        tool_logs=tool_logs,
        input_payload=task["input_payload"],
    )

    assert res.scores.task_success == 1.0
    assert res.scores.policy == 1.0
    assert res.scores.evidence == 1.0
    assert res.scores.task_aggregate >= 0.85
