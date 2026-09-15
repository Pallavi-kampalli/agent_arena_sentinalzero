from typing import Any

from agent_arena.tasks.generator import FAMILIES, VARIANTS
from agent_arena.tasks.solver import ReferenceSolver


def validate_task(task: dict[str, Any]) -> tuple[bool, str | None]:
    """Validates a generated task per PRD §8 and SupportOps_PS_v2.md.

    Rejects tasks that are:
    - Structurally incomplete or malformed
    - Lacking customer or entity grounding in world_state_seed
    - Citing phantom/hallucinated evidence IDs
    - Inconsistent with the canonical reference solver
    - Ambiguous (e.g. conflicting policies with identical timestamps)
    - Unsolvable (e.g. missing target action transaction/subscription or invalid amounts)
    - Internally inconsistent (e.g. orphan transactions/subscriptions not referencing valid customers)

    Returns (is_valid, error_reason).
    """
    # 1. Structural checks
    required_keys = {"task_id", "dataset", "family", "variant", "input_payload", "world_state_seed", "ground_truth"}
    missing = required_keys - set(task.keys())
    if missing:
        return False, f"Missing required top-level task keys: {missing}"

    if task["dataset"] not in {"dev", "hidden"}:
        return False, f"Invalid dataset '{task['dataset']}'. Must be 'dev' or 'hidden'"

    if task["family"] not in FAMILIES:
        return False, f"Invalid family '{task['family']}'. Must be one of {FAMILIES}"

    if task["variant"] not in VARIANTS:
        return False, f"Invalid variant '{task['variant']}'. Must be one of {VARIANTS}"

    # 2. Input payload checks
    input_payload = task["input_payload"]
    if not isinstance(input_payload, dict):
        return False, "input_payload must be a dictionary"

    cust_id = input_payload.get("customer_id")
    cust_msg = input_payload.get("customer_message")
    if not cust_id or not isinstance(cust_id, str):
        return False, "input_payload.customer_id must be a non-empty string"
    if not cust_msg or not isinstance(cust_msg, str):
        return False, "input_payload.customer_message must be a non-empty string"

    # 3. World state seed entity verification & referential integrity
    world = task["world_state_seed"]
    if not isinstance(world, dict):
        return False, "world_state_seed must be a dictionary"

    customers = {c["id"]: c for c in world.get("customers", [])}
    if cust_id not in customers:
        return False, f"Target customer_id '{cust_id}' does not exist in world_state_seed"

    # Referential integrity: check all transactions point to existing customers & have valid positive amounts
    for tx in world.get("transactions", []):
        tx_cust = tx.get("customer_id")
        if tx_cust not in customers:
            return (
                False,
                f"Internally inconsistent world state: Transaction '{tx.get('id')}' references non-existent customer '{tx_cust}'",
            )
        amt = tx.get("amount")
        if amt is None or amt <= 0:
            return False, f"Unsolvable task: Transaction '{tx.get('id')}' has invalid non-positive amount: {amt}"

    # Referential integrity: check all subscriptions point to existing customers
    for sub in world.get("subscriptions", []):
        sub_cust = sub.get("customer_id")
        if sub_cust not in customers:
            return (
                False,
                f"Internally inconsistent world state: Subscription '{sub.get('id')}' references non-existent customer '{sub_cust}'",
            )

    # 4. Ambiguity check: no conflicting policies in the same category sharing the exact same updated_at
    policies_by_cat: dict[str, list[dict]] = {}
    for pol in world.get("policies", []):
        cat = pol.get("category", "general")
        policies_by_cat.setdefault(cat, []).append(pol)

    for cat, pol_list in policies_by_cat.items():
        if len(pol_list) > 1:
            timestamps = [p.get("updated_at") for p in pol_list]
            if len(timestamps) != len(set(timestamps)):
                # Two policies in same category have identical updated_at
                return (
                    False,
                    f"Ambiguous task: category '{cat}' contains conflicting policies with identical updated_at timestamp",
                )

    # 5. Ground truth structure checks
    gt = task["ground_truth"]
    if not isinstance(gt, dict):
        return False, "ground_truth must be a dictionary"

    gt_required = {"expected_resolution", "must_escalate", "required_evidence", "expected_action", "expected_end_state"}
    missing_gt = gt_required - set(gt.keys())
    if missing_gt:
        return False, f"Missing ground_truth keys: {missing_gt}"

    # 6. Solvability check for referenced entities and expected action targets
    # Key semantic rule:
    # - missing referenced transaction/subscription/customer -> invalid / unsolvable task
    # - existing transaction/subscription that is ineligible -> valid task with a legitimate negative eligibility result
    meta = task.get("task_meta", {})
    all_tx_ids = {t["id"] for t in world.get("transactions", [])}
    all_sub_ids = {s["id"] for s in world.get("subscriptions", [])}

    if "transaction_id" in meta:
        ref_tx = meta["transaction_id"]
        if ref_tx and ref_tx not in all_tx_ids:
            return False, f"Unsolvable task: referenced transaction '{ref_tx}' does not exist in world_state_seed"

    if "subscription_id" in meta:
        ref_sub = meta["subscription_id"]
        if ref_sub and ref_sub not in all_sub_ids:
            return False, f"Unsolvable task: referenced subscription '{ref_sub}' does not exist in world_state_seed"

    action = gt.get("expected_action", {})
    action_tool = action.get("tool")
    if action_tool == "issue_refund":
        target_tx = action.get("params", {}).get("transaction_id")
        if not target_tx or target_tx not in all_tx_ids:
            return (
                False,
                f"Unsolvable task: expected action 'issue_refund' targets non-existent transaction '{target_tx}'",
            )
    elif action_tool == "cancel_subscription":
        target_sub = action.get("params", {}).get("subscription_id")
        if not target_sub or target_sub not in all_sub_ids:
            return (
                False,
                f"Unsolvable task: expected action 'cancel_subscription' targets non-existent subscription '{target_sub}'",
            )

    # 7. Evidence grounding check: all cited evidence IDs must exist in world state
    all_world_ids = set(customers.keys())
    for tx in world.get("transactions", []):
        all_world_ids.add(tx.get("id"))
    for sub in world.get("subscriptions", []):
        all_world_ids.add(sub.get("id"))
    for pol in world.get("policies", []):
        all_world_ids.add(pol.get("id"))
    for doc in world.get("documents", []):
        all_world_ids.add(doc.get("id"))
    for cs in world.get("historical_cases", []):
        all_world_ids.add(cs.get("case_id"))

    for evid_id in gt["required_evidence"]:
        if evid_id not in all_world_ids:
            return False, f"Ground truth required_evidence cites non-existent entity ID: '{evid_id}'"

    # 8. Re-solve with reference solver to ensure determinism and zero-drift
    try:
        derived_gt = ReferenceSolver.solve(
            world_state_seed=world,
            input_payload=input_payload,
            family=task["family"],
            variant=task["variant"],
            task_meta=meta,
        )
    except Exception as e:
        return False, f"Reference solver failed to derive ground truth: {e}"

    derived_dict = derived_gt.to_dict()
    if derived_dict["expected_resolution"] != gt["expected_resolution"]:
        return False, (
            f"Ground truth expected_resolution mismatch: "
            f"derived '{derived_dict['expected_resolution']}' vs stored '{gt['expected_resolution']}'"
        )

    if derived_dict["must_escalate"] != gt["must_escalate"]:
        return False, (
            f"Ground truth must_escalate mismatch: "
            f"derived {derived_dict['must_escalate']} vs stored {gt['must_escalate']}"
        )

    if derived_dict["required_evidence"] != gt["required_evidence"]:
        return False, (
            f"Ground truth required_evidence mismatch: "
            f"derived {derived_dict['required_evidence']} vs stored {gt['required_evidence']}"
        )

    if derived_dict["expected_action"]["tool"] != gt["expected_action"].get("tool"):
        return False, (
            f"Ground truth expected_action tool mismatch: "
            f"derived '{derived_dict['expected_action']['tool']}' vs stored '{gt['expected_action'].get('tool')}'"
        )

    return True, None
