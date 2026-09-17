"""tests/test_sentinelzero_scoring.py

28 focused scoring tests covering all 7 dimensions plus edge cases.
Tests operate entirely in-memory (no DB, no network).
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "starter-kit"))
sys.path.insert(0, str(ROOT))

import unittest

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
from agent_arena.scoring.sentinelzero_evaluator import (
    SENTINELZERO_WEIGHTS,
    SentinelZeroTaskEvaluator,
)


# ──────────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────────

def _make_submission(
    resolution: str,
    evidence: list[str],
    confidence: float = 0.9,
    summary: str = "Quarantined suspicious email from external sender.",
    escalation_required: bool = False,
) -> dict:
    return {
        "status": "completed",
        "submission_payload": {
            "task_id": "T-TEST",
            "decision": {
                "resolution": resolution,
                "escalation_required": escalation_required,
            },
            "evidence": evidence,
            "customer_response": summary,
            "confidence": confidence,
        },
    }


def _make_gt(
    expected_res: str,
    must_escalate: bool = False,
    required_evidence: list[str] | None = None,
    message_id: str = "MSG-T-001",
) -> dict:
    return {
        "expected_resolution": expected_res,
        "must_escalate": must_escalate,
        "required_evidence": required_evidence or [message_id],
        "expected_action": {
            "tool": DECISION_TO_ACTION[expected_res],
            "params": {"message_id": message_id},
        },
    }


def _make_logs(action_tool: str, message_id: str, extra_ids: list[str] | None = None) -> list[dict]:
    """Builds synthetic tool logs that would produce the given action + observed IDs."""
    payload = {"message_id": message_id}
    logs = [
        {
            "tool_name": "get_email_headers",
            "request_payload": {"message_id": message_id},
            "response_payload": {"message_id": message_id, "auth_results": {"spf": "pass"}},
            "was_enforcement_rejection": False,
        },
        {
            "tool_name": "get_approved_domains",
            "request_payload": {},
            "response_payload": {"official_domains": ["sentinel-acme.edu"]},
            "was_enforcement_rejection": False,
        },
        {
            "tool_name": action_tool,
            "request_payload": payload,
            "response_payload": {"status": "ok", **({f: f for f in (extra_ids or [])})},
            "was_enforcement_rejection": False,
        },
    ]
    return logs


# ──────────────────────────────────────────────────────────────────────────────
# Tests
# ──────────────────────────────────────────────────────────────────────────────

class TestTaskSuccess(unittest.TestCase):

    def test_1_perfect_allow(self):
        """1. Perfect ALLOW case → task_success = 1.0."""
        s = sz_score_task_success("allow", "allow", "allow_and_deliver", "allow_and_deliver", "MSG-1", "MSG-1")
        self.assertEqual(s, 1.0)

    def test_2_perfect_warn(self):
        """2. Perfect WARN case → task_success = 1.0."""
        s = sz_score_task_success("warn", "warn", "apply_warning_banner", "apply_warning_banner", "MSG-2", "MSG-2")
        self.assertEqual(s, 1.0)

    def test_3_perfect_quarantine(self):
        """3. Perfect QUARANTINE case → task_success = 1.0."""
        s = sz_score_task_success("quarantine", "quarantine", "quarantine_message", "quarantine_message", "MSG-3", "MSG-3")
        self.assertEqual(s, 1.0)

    def test_4_perfect_escalate(self):
        """4. Perfect ESCALATE case → task_success = 1.0."""
        s = sz_score_task_success("escalate", "escalate", "escalate_to_tier2_soc", "escalate_to_tier2_soc", "MSG-4", "MSG-4")
        self.assertEqual(s, 1.0)

    def test_5_wrong_decision_allow_on_quarantine(self):
        """5. ALLOW when QUARANTINE is expected → safety critical failure → 0.0."""
        s = sz_score_task_success("allow", "quarantine", "allow_and_deliver", "quarantine_message", "MSG-5", "MSG-5")
        self.assertEqual(s, 0.0)

    def test_5b_wrong_decision_allow_on_escalate(self):
        """5b. ALLOW when ESCALATE is expected → safety critical failure → 0.0."""
        s = sz_score_task_success("allow", "escalate", "allow_and_deliver", "escalate_to_tier2_soc", "MSG-5b", "MSG-5b")
        self.assertEqual(s, 0.0)

    def test_6_correct_decision_wrong_action(self):
        """6. Correct decision/wrong action → substantial partial credit (0.55)."""
        s = sz_score_task_success("quarantine", "quarantine", "allow_and_deliver", "quarantine_message", "MSG-6", "MSG-6")
        self.assertAlmostEqual(s, 0.55, places=5)

    def test_7_quarantine_when_escalate_expected(self):
        """7. QUARANTINE when ESCALATE expected → partial (both block decisions)."""
        s = sz_score_task_success("quarantine", "escalate", "quarantine_message", "escalate_to_tier2_soc", "MSG-7", "MSG-7")
        self.assertGreater(s, 0.0)
        self.assertLess(s, 1.0)

    def test_8_escalate_is_not_mapped_to_quarantine(self):
        """8. ESCALATE must NOT be treated as QUARANTINE in action mapping."""
        self.assertNotEqual(DECISION_TO_ACTION["escalate"], "quarantine_message")
        self.assertEqual(DECISION_TO_ACTION["escalate"], "escalate_to_tier2_soc")

    def test_9_invalid_decision_returns_zero(self):
        """9. Invalid decision string → 0.0."""
        s = sz_score_task_success("refund", "quarantine", None, "quarantine_message")
        self.assertEqual(s, 0.0)


class TestPolicyScoring(unittest.TestCase):

    def test_10_policy_allow_when_quarantine_expected_is_zero(self):
        """10. ALLOW when QUARANTINE expected → policy = 0.0 (critical violation)."""
        s = sz_score_policy(0, "allow", "quarantine", action_matches_decision=True)
        self.assertEqual(s, 0.0)

    def test_11_policy_perfect_quarantine(self):
        """11. Perfect QUARANTINE decision + correct action → high policy score."""
        s = sz_score_policy(0, "quarantine", "quarantine", action_matches_decision=True)
        self.assertGreater(s, 0.8)

    def test_12_policy_enforcement_rejection_penalty(self):
        """12. Each enforcement rejection applies 25% penalty."""
        s0 = sz_score_policy(0, "quarantine", "quarantine", True)
        s1 = sz_score_policy(1, "quarantine", "quarantine", True)
        self.assertLess(s1, s0)
        self.assertAlmostEqual(s1 / s0, 0.75, places=5)


class TestEvidenceScoring(unittest.TestCase):

    MSG_ID = "MSG-T-001"
    EMP_ID = "EMP-1001"
    DOM_ID = "DOM-101"

    def test_13_valid_evidence_is_credited(self):
        """13. Valid observed evidence cited → good F1 score."""
        observed = {self.MSG_ID, self.EMP_ID, self.DOM_ID}
        f1, prec, rec, tp = sz_score_evidence(
            submitted_evidence=[self.MSG_ID, self.EMP_ID],
            required_evidence=[self.MSG_ID, self.EMP_ID],
            observed_evidence=observed,
        )
        self.assertEqual(f1, 1.0)

    def test_14_fabricated_evidence_is_penalized(self):
        """14. Fabricated/nonexistent IDs → counted as false positives, lower precision."""
        observed = {self.MSG_ID}
        f1, prec, rec, tp = sz_score_evidence(
            submitted_evidence=[self.MSG_ID, "EMP-9999"],  # EMP-9999 not observed
            required_evidence=[self.MSG_ID],
            observed_evidence=observed,
        )
        # Precision should drop because EMP-9999 is fabricated
        self.assertLess(prec, 1.0)

    def test_15_empty_evidence_on_no_required(self):
        """15. No required evidence + no submission → perfect score."""
        f1, prec, rec, tp = sz_score_evidence([], [], set())
        self.assertEqual(f1, 1.0)


class TestCalibrationScoring(unittest.TestCase):

    def test_16_high_confidence_on_correct_is_rewarded(self):
        """16. High confidence when correct → calibration near confidence value."""
        s = sz_score_calibration(0.95, task_success=1.0, must_escalate=False, escalation_required=False)
        self.assertGreater(s, 0.8)

    def test_17_invalid_confidence_returns_zero(self):
        """17. Invalid confidence (None/non-numeric/out-of-range) → 0.0."""
        self.assertEqual(sz_score_calibration(None, 1.0, False, False), 0.0)
        self.assertEqual(sz_score_calibration("high", 1.0, False, False), 0.0)
        self.assertEqual(sz_score_calibration(1.5, 1.0, False, False), 0.0)
        self.assertEqual(sz_score_calibration(-0.1, 1.0, False, False), 0.0)

    def test_18_constant_05_confidence_scores_half(self):
        """18. Always returning 0.5 confidence on correct → only 0.5 credit (non-trivial)."""
        s_correct = sz_score_calibration(0.5, task_success=1.0, must_escalate=False, escalation_required=False)
        s_wrong = sz_score_calibration(0.5, task_success=0.0, must_escalate=False, escalation_required=False)
        # Both at 0.5 should differ (correct > wrong is not guaranteed if both are 0.5
        # but neither should be maximum score to prevent gaming with 0.5)
        self.assertLessEqual(s_correct, 0.5 + 1e-9)


class TestEfficiencyScoring(unittest.TestCase):

    def test_19_minimal_calls_perfect_efficiency(self):
        """19. 4 tool calls (under sweet spot of 6) → high efficiency."""
        s = sz_score_efficiency(tool_calls_count=4, duplicate_calls_count=0, budget=10, task_success=1.0)
        self.assertEqual(s, 1.0)

    def test_20_redundant_calls_penalize_efficiency(self):
        """20. Redundant (duplicate) tool calls → efficiency penalty."""
        s_no_dup = sz_score_efficiency(5, 0, 10, 1.0)
        s_with_dup = sz_score_efficiency(5, 2, 10, 1.0)
        self.assertLess(s_with_dup, s_no_dup)

    def test_21_exceeding_budget_returns_zero(self):
        """21. Exceeding tool call budget → 0.0 efficiency."""
        s = sz_score_efficiency(tool_calls_count=15, duplicate_calls_count=0, budget=10, task_success=1.0)
        self.assertEqual(s, 0.0)


class TestCommunicationScoring(unittest.TestCase):

    def test_22_concise_grounded_summary_scores_well(self):
        """22. Concise useful summary with domain keywords → high communication score."""
        s = sz_score_communication(
            summary="External sender used a phishing domain. Quarantined message MSG-001.",
            submitted_resolution="quarantine",
            expected_resolution="quarantine",
            task_success=1.0,
        )
        self.assertGreater(s, 0.6)

    def test_23_empty_summary_scores_low(self):
        """23. Empty/missing summary → low communication score."""
        s = sz_score_communication("", "quarantine", "quarantine", 1.0)
        self.assertLess(s, 0.3)

    def test_24_false_claim_quarantine_when_allowed(self):
        """24. Summary says 'quarantined' but decision was 'allow' → false claim penalty."""
        s = sz_score_communication(
            summary="This email was quarantined due to suspicious sender.",
            submitted_resolution="allow",
            expected_resolution="quarantine",
            task_success=0.0,
        )
        # Should lose the 'no false claims' point
        self.assertLess(s, 0.75)


class TestRobustnessScoring(unittest.TestCase):

    def test_25_malformed_submission_returns_low_score(self):
        """25. Malformed submission (invalid decision) → low robustness."""
        s = sz_score_robustness(task_success=0.0, invalid_decision=True)
        self.assertEqual(s, 0.0)

    def test_26_fabricated_evidence_reduces_robustness(self):
        """26. Fabricated evidence items reduce robustness."""
        s_clean = sz_score_robustness(task_success=1.0, fabricated_evidence_count=0)
        s_fabricated = sz_score_robustness(task_success=1.0, fabricated_evidence_count=2)
        self.assertLess(s_fabricated, s_clean)

    def test_27_multiple_actions_penalize_robustness(self):
        """27. Multiple contradictory actions reduce robustness."""
        s_single = sz_score_robustness(task_success=1.0, multiple_actions_taken=1)
        s_multi = sz_score_robustness(task_success=1.0, multiple_actions_taken=3)
        self.assertLess(s_multi, s_single)


class TestFullEvaluatorPipeline(unittest.TestCase):
    """Tests that exercise the full SentinelZeroTaskEvaluator pipeline."""

    WORLD_SEED = {}
    MSG_ID = "MSG-TEST-01"

    def _eval(self, sub_res, exp_res, confidence=0.9, extra_logs=None, evidence=None):
        action_tool = DECISION_TO_ACTION[sub_res]
        gt = _make_gt(exp_res, message_id=self.MSG_ID)
        logs = _make_logs(action_tool, self.MSG_ID)
        if extra_logs:
            logs.extend(extra_logs)
        sub_ev = evidence if evidence is not None else [self.MSG_ID]
        sub_rec = _make_submission(sub_res, sub_ev, confidence)
        return SentinelZeroTaskEvaluator.evaluate_task(
            task_id="T-EVAL",
            world_seed=self.WORLD_SEED,
            ground_truth=gt,
            runtime_state=None,
            submission_record=sub_rec,
            tool_logs=logs,
            input_payload={"message_id": self.MSG_ID},
        )

    def test_28_final_score_weighting(self):
        """28. Final score weighting: confirm SentinelZero weights sum to 1.0."""
        total = sum(SENTINELZERO_WEIGHTS.values())
        self.assertAlmostEqual(total, 1.0, places=10)

    def test_28b_sentinelzero_weights_are_correct(self):
        """28b. Verify exact SentinelZero weight values."""
        self.assertAlmostEqual(SENTINELZERO_WEIGHTS["task_success"], 0.35)
        self.assertAlmostEqual(SENTINELZERO_WEIGHTS["policy"], 0.15)
        self.assertAlmostEqual(SENTINELZERO_WEIGHTS["evidence"], 0.15)
        self.assertAlmostEqual(SENTINELZERO_WEIGHTS["calibration"], 0.10)
        self.assertAlmostEqual(SENTINELZERO_WEIGHTS["efficiency"], 0.10)
        self.assertAlmostEqual(SENTINELZERO_WEIGHTS["communication"], 0.10)
        self.assertAlmostEqual(SENTINELZERO_WEIGHTS["robustness"], 0.05)

    def test_28c_scores_within_0_100(self):
        """28c. All dimension scores are in [0.0, 1.0] and aggregate is in [0.0, 1.0]."""
        result = self._eval("quarantine", "quarantine")
        s = result.scores
        for dim in ["task_success", "policy", "evidence", "calibration", "efficiency", "communication", "robustness", "task_aggregate"]:
            val = getattr(s, dim)
            self.assertGreaterEqual(val, 0.0, f"{dim} < 0.0")
            self.assertLessEqual(val, 1.0, f"{dim} > 1.0")

    def test_28d_no_nan_or_inf(self):
        """28d. No NaN or Infinity in scores."""
        import math
        result = self._eval("quarantine", "quarantine")
        s = result.scores.model_dump()
        for k, v in s.items():
            self.assertFalse(math.isnan(float(v)), f"{k} is NaN")
            self.assertFalse(math.isinf(float(v)), f"{k} is Inf")

    def test_28e_determinism(self):
        """28e. Identical inputs produce identical scores (deterministic)."""
        r1 = self._eval("quarantine", "quarantine")
        r2 = self._eval("quarantine", "quarantine")
        self.assertEqual(r1.scores.model_dump(), r2.scores.model_dump())


if __name__ == "__main__":
    unittest.main(verbosity=2)
