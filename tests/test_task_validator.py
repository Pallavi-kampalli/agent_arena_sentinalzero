import copy
import pytest
from agent_arena.tasks.generator import TaskGenerator
from agent_arena.tasks.validator import validate_task
from agent_arena.world.generator import generate_world


@pytest.fixture
def valid_task():
    world = generate_world(seed=42)
    gen = TaskGenerator(seed=42)
    return gen.generate_task(
        base_world=world,
        family="refund_request",
        variant="normal",
        task_id="TASK-DEV-TEST01",
        dataset="dev",
    )


def test_validate_task_success(valid_task):
    """Verify that a properly generated task passes validation."""
    is_valid, err = validate_task(valid_task)
    assert is_valid is True
    assert err is None


def test_validate_task_missing_keys(valid_task):
    """Verify task rejection when top-level keys are missing."""
    corrupted = copy.deepcopy(valid_task)
    del corrupted["ground_truth"]

    is_valid, err = validate_task(corrupted)
    assert is_valid is False
    assert "Missing required top-level task keys" in err


def test_validate_task_nonexistent_customer(valid_task):
    """Verify rejection when input_payload.customer_id is not in world_state_seed."""
    corrupted = copy.deepcopy(valid_task)
    corrupted["input_payload"]["customer_id"] = "CUS-NONEXISTENT-9999"

    is_valid, err = validate_task(corrupted)
    assert is_valid is False
    assert "does not exist in world_state_seed" in err


def test_validate_task_phantom_evidence_id(valid_task):
    """Verify rejection when ground truth cites a non-existent evidence ID."""
    corrupted = copy.deepcopy(valid_task)
    corrupted["ground_truth"]["required_evidence"].append("DOC-PHANTOM-9999")

    is_valid, err = validate_task(corrupted)
    assert is_valid is False
    assert "cites non-existent entity ID" in err


def test_validate_task_ground_truth_drift(valid_task):
    """Verify rejection when stored ground truth contradicts derived ground truth."""
    corrupted = copy.deepcopy(valid_task)
    # Tamper with resolution
    corrupted["ground_truth"]["expected_resolution"] = "deny" if valid_task["ground_truth"]["expected_resolution"] == "refund" else "refund"

    is_valid, err = validate_task(corrupted)
    assert is_valid is False
    assert "Ground truth expected_resolution mismatch" in err


def test_validate_task_ambiguous_conflicting_policies(valid_task):
    """Verify rejection when category contains conflicting policies with identical updated_at."""
    corrupted = copy.deepcopy(valid_task)
    pols = corrupted["world_state_seed"]["policies"]
    # Find refund policies or add a duplicate with same updated_at
    ts = "2024-06-01T00:00:00Z"
    pols.append({
        "id": "DOC-CONFLICT-01",
        "title": "Conflicting Policy 1",
        "category": "refund",
        "content": "Conflicting content A",
        "updated_at": ts,
        "is_active": True,
    })
    pols.append({
        "id": "DOC-CONFLICT-02",
        "title": "Conflicting Policy 2",
        "category": "refund",
        "content": "Conflicting content B",
        "updated_at": ts,
        "is_active": True,
    })

    is_valid, err = validate_task(corrupted)
    assert is_valid is False
    assert "Ambiguous task" in err
    assert "identical updated_at timestamp" in err


def test_validate_task_unsolvable_action_target(valid_task):
    """Verify rejection when expected action targets a non-existent transaction."""
    corrupted = copy.deepcopy(valid_task)
    if corrupted["ground_truth"]["expected_action"]["tool"] == "issue_refund":
        corrupted["ground_truth"]["expected_action"]["params"]["transaction_id"] = "TX-NONEXISTENT-9999"
        is_valid, err = validate_task(corrupted)
        assert is_valid is False
        assert "Unsolvable task" in err
        assert "non-existent transaction" in err


def test_validate_task_inconsistent_orphan_transaction(valid_task):
    """Verify rejection when a transaction references a non-existent customer."""
    corrupted = copy.deepcopy(valid_task)
    if corrupted["world_state_seed"]["transactions"]:
        corrupted["world_state_seed"]["transactions"][0]["customer_id"] = "CUS-ORPHAN-9999"
        is_valid, err = validate_task(corrupted)
        assert is_valid is False
        assert "Internally inconsistent world state" in err
        assert "non-existent customer" in err


def test_validate_task_invalid_transaction_amount(valid_task):
    """Verify rejection when a transaction has non-positive amount."""
    corrupted = copy.deepcopy(valid_task)
    if corrupted["world_state_seed"]["transactions"]:
        corrupted["world_state_seed"]["transactions"][0]["amount"] = -25.0
        is_valid, err = validate_task(corrupted)
        assert is_valid is False
        assert "Unsolvable task" in err
        assert "invalid non-positive amount" in err


def test_validate_task_existing_ineligible_refund_target_passes():
    """Verify: existing transaction that is ineligible for refund -> validate_task() == PASS."""
    world = generate_world(seed=42)
    gen = TaskGenerator(seed=42)
    # contradiction variant places the target transaction under active chargeback hold
    task = gen.generate_task(
        base_world=world,
        family="refund_request",
        variant="contradiction",
        task_id="TASK-DEV-INELIGIBLE-01",
        dataset="dev",
    )

    # Confirm the target transaction exists in world state
    target_tx_id = task["task_meta"]["transaction_id"]
    matching_txs = [t for t in task["world_state_seed"]["transactions"] if t["id"] == target_tx_id]
    assert len(matching_txs) == 1
    # Confirm it is genuinely ineligible (active chargeback hold)
    assert matching_txs[0]["chargeback_status"] == "investigation_active"
    # Confirm the ground truth reflects a legitimate negative eligibility result
    assert task["ground_truth"]["expected_resolution"] in {"deny", "escalate"}

    # An existing but ineligible target is a valid benchmark task: validate_task must PASS
    is_valid, err = validate_task(task)
    assert is_valid is True, f"Expected validate_task to PASS for ineligible target, got error: {err}"
    assert err is None


def test_validate_task_missing_refund_target_fails():
    """Verify: missing referenced transaction -> validate_task() == FAIL (unsolvable)."""
    world = generate_world(seed=42)
    gen = TaskGenerator(seed=42)
    task = gen.generate_task(
        base_world=world,
        family="refund_request",
        variant="normal",
        task_id="TASK-DEV-MISSING-TARGET-01",
        dataset="dev",
    )

    target_tx_id = task["task_meta"]["transaction_id"]
    # Remove the target transaction from world_state_seed
    task["world_state_seed"]["transactions"] = [
        t for t in task["world_state_seed"]["transactions"] if t["id"] != target_tx_id
    ]

    # When the referenced target is missing, the task is unsolvable and must FAIL validation
    is_valid, err = validate_task(task)
    assert is_valid is False
    assert "Unsolvable task" in err
    assert f"'{target_tx_id}'" in err


