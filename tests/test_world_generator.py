import pytest
from agent_arena.world.generator import WorldGenerator, generate_world
from agent_arena.domain.rules import parse_iso


def test_world_generator_determinism():
    """Verify generate_world(seed) produces 100% identical outputs for same seed."""
    world_a = generate_world(seed=123)
    world_b = generate_world(seed=123)

    assert world_a["seed"] == world_b["seed"]
    assert len(world_a["customers"]) == len(world_b["customers"])
    assert len(world_a["transactions"]) == len(world_b["transactions"])
    assert len(world_a["subscriptions"]) == len(world_b["subscriptions"])
    assert world_a["customers"][0] == world_b["customers"][0]
    assert world_a["transactions"][0] == world_b["transactions"][0]


def test_world_generator_seed_diversity():
    """Verify different seeds produce distinct worlds."""
    world_a = generate_world(seed=101)
    world_b = generate_world(seed=202)

    assert world_a["customers"][0]["id"] != world_b["customers"][0]["id"] or \
           world_a["customers"][0]["name"] != world_b["customers"][0]["name"]


def test_world_entities_structure():
    """Verify all required SupportOps entity domains are present with required fields."""
    world = generate_world(seed=42)

    # Customers
    assert len(world["customers"]) >= 50
    cust = world["customers"][0]
    for key in ["id", "name", "email", "tier", "region", "verification_status", "account_status"]:
        assert key in cust

    # Transactions
    assert len(world["transactions"]) >= 100
    tx = world["transactions"][0]
    for key in ["id", "customer_id", "amount", "currency", "date", "status", "chargeback_status", "refund_status"]:
        assert key in tx

    # Subscriptions
    assert len(world["subscriptions"]) >= 50
    sub = world["subscriptions"][0]
    for key in ["id", "customer_id", "plan", "billing_cycle", "status", "start_date"]:
        assert key in sub

    # Policies
    assert len(world["policies"]) >= 8
    doc_ids = {p["id"] for p in world["policies"]}
    assert "DOC-1001" in doc_ids  # Authoritative refund policy
    assert "DOC-1842" in doc_ids  # Chargeback hold policy
    assert "DOC-0991" in doc_ids  # Stale refund policy


def test_authoritative_vs_stale_policy_timestamps():
    """Verify stale policies have older updated_at timestamps than authoritative ones."""
    world = generate_world(seed=42)
    doc_1001 = next(p for p in world["policies"] if p["id"] == "DOC-1001")
    doc_0991 = next(p for p in world["policies"] if p["id"] == "DOC-0991")

    dt_current = parse_iso(doc_1001["updated_at"])
    dt_stale = parse_iso(doc_0991["updated_at"])

    assert dt_current > dt_stale
    assert doc_1001["is_authoritative"] is True
    assert doc_0991["is_authoritative"] is False


def test_historical_cases_contain_previous_wrong_actions():
    """Verify historical cases include past incorrect agent actions (Previous-Agent-Was-Wrong)."""
    world = generate_world(seed=42)
    cases = world["historical_cases"]
    assert len(cases) >= 20

    wrong_cases = [c for c in cases if c.get("was_correct") is False]
    correct_cases = [c for c in cases if c.get("was_correct") is True]

    assert len(wrong_cases) > 0, "World must contain historical cases where previous agent was wrong"
    assert len(correct_cases) > 0
