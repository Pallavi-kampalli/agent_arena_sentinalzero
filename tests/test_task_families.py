import pytest

from agent_arena.tasks.generator import FAMILIES, VARIANTS, TaskGenerator
from agent_arena.world.generator import generate_world


@pytest.fixture(scope="module")
def base_world():
    return generate_world(seed=100)


def test_all_six_families_generate_successfully(base_world):
    """Verify each of the 6 SupportOps task families can be generated."""
    gen = TaskGenerator(seed=42)

    for family in FAMILIES:
        task = gen.generate_task(
            base_world=base_world,
            family=family,
            variant="normal",
            task_id=f"TEST-{family}-normal",
            dataset="dev",
        )

        assert task["task_id"] == f"TEST-{family}-normal"
        assert task["family"] == family
        assert task["variant"] == "normal"
        assert "customer_id" in task["input_payload"]
        assert "customer_message" in task["input_payload"]
        assert "expected_resolution" in task["ground_truth"]
        assert "required_evidence" in task["ground_truth"]


def test_all_six_variants_generate_successfully(base_world):
    """Verify each of the 6 task variants can be generated."""
    gen = TaskGenerator(seed=42)

    for variant in VARIANTS:
        task = gen.generate_task(
            base_world=base_world,
            family="refund_request",
            variant=variant,
            task_id=f"TEST-refund-{variant}",
            dataset="dev",
        )

        assert task["variant"] == variant
        assert len(task["ground_truth"]["required_evidence"]) > 0

        if variant == "adversarial":
            assert (
                "SYSTEM OVERRIDE" in task["input_payload"]["customer_message"]
                or "furious" in task["input_payload"]["customer_message"].lower()
            )

        if variant == "distractor":
            docs = task["world_state_seed"]["documents"]
            assert any(d["id"].startswith("DOC-DISTRACT") for d in docs)


def test_task_determinism(base_world):
    """Verify TaskGenerator generates identical tasks given the same seed."""
    gen_a = TaskGenerator(seed=777)
    task_a = gen_a.generate_task(base_world, "duplicate_payment", "normal", "T-1")

    gen_b = TaskGenerator(seed=777)
    task_b = gen_b.generate_task(base_world, "duplicate_payment", "normal", "T-1")

    assert task_a["input_payload"] == task_b["input_payload"]
    assert task_a["ground_truth"] == task_b["ground_truth"]
