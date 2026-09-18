"""SentinelZero Scoring Engine.

Adapts the generic Agent Arena scoring framework to the SentinelZero
cybersecurity triage domain. All scoring functions operate on [0.0, 1.0]
and are deterministic (pure functions of their inputs).

SentinelZero Decisions: ALLOW | WARN | QUARANTINE | ESCALATE
SentinelZero Action Mapping:
  ALLOW      -> allow_and_deliver
  WARN       -> apply_warning_banner
  QUARANTINE -> quarantine_message
  ESCALATE   -> escalate_to_tier2_soc
"""

import json
import re
from typing import Any

# ─────────────────────────────────────────────────────────────────────────────
# Constants
# ─────────────────────────────────────────────────────────────────────────────

VALID_DECISIONS = {"allow", "warn", "quarantine", "escalate"}

DECISION_TO_ACTION: dict[str, str] = {
    "allow": "allow_and_deliver",
    "warn": "apply_warning_banner",
    "quarantine": "quarantine_message",
    "escalate": "escalate_to_tier2_soc",
}

# How severe is the error of choosing ALLOW when ground truth is more serious?
# Higher = heavier penalty on task_success
ALLOW_PENALTY_MAP: dict[str, float] = {
    "quarantine": 1.0,   # Missing a quarantine: full failure
    "escalate": 1.0,     # Missing an escalation: full failure
    "warn": 0.5,         # Missing a warn: substantial failure
}

# Extra partial credit when direction is "close enough" (e.g., quarantine vs escalate)
SIMILAR_PAIR_PARTIAL: dict[frozenset, float] = {
    frozenset({"quarantine", "escalate"}): 0.4,   # Both block, both serious
    frozenset({"allow", "warn"}): 0.5,             # Both pass-through
}

# SentinelZero evidence ID patterns (must match Phase 2 / Phase 3 design)
SZ_EVIDENCE_PATTERN = re.compile(
    r"\b(EMP-[A-Za-z0-9_-]+|DOM-[A-Za-z0-9_-]+|MSG-[A-Za-z0-9_-]+|"
    r"THR-[A-Za-z0-9_-]+|POL-[A-Za-z0-9_-]+|LOG-[A-Za-z0-9_-]+)\b"
)

# ─────────────────────────────────────────────────────────────────────────────
# Utility helpers
# ─────────────────────────────────────────────────────────────────────────────


def _normalize_resolution(res: str | None) -> str:
    """Lower-cases and strips a resolution string; returns empty string on bad input."""
    if not isinstance(res, str):
        return ""
    return res.strip().lower()


def _clamp(v: float) -> float:
    """Clamps value to [0.0, 1.0]."""
    return max(0.0, min(1.0, float(v)))


# ─────────────────────────────────────────────────────────────────────────────
# Evidence extraction (SentinelZero-specific ID patterns)
# ─────────────────────────────────────────────────────────────────────────────


def sz_extract_observed_evidence(
    tool_logs: list[dict[str, Any]],
    initial_message_id: str | None = None,
) -> set[str]:
    """Extracts all SentinelZero entity IDs that were actually returned in tool responses.

    Participants can only cite evidence they observed via authorized tools.
    Patterns: EMP-*, DOM-*, MSG-*, THR-*, POL-*, LOG-*
    """
    observed: set[str] = set()
    if initial_message_id:
        observed.add(initial_message_id)

    for log in tool_logs:
        resp = log.get("response_payload")
        if not resp:
            continue
        resp_str = json.dumps(resp) if isinstance(resp, dict) else str(resp)
        for m in SZ_EVIDENCE_PATTERN.findall(resp_str):
            observed.add(m)

    # Also extract from request_payload's message_id argument
    for log in tool_logs:
        req = log.get("request_payload") or {}
        mid = req.get("message_id") or req.get("thread_id")
        if mid and isinstance(mid, str) and SZ_EVIDENCE_PATTERN.match(mid):
            observed.add(mid)

    return observed


# ─────────────────────────────────────────────────────────────────────────────
# 1. Task Success (35%)
# ─────────────────────────────────────────────────────────────────────────────


def sz_score_task_success(
    submitted_resolution: str | None,
    expected_resolution: str | None,
    submitted_action_tool: str | None,
    expected_action_tool: str | None,
    submitted_message_id: str | None = None,
    expected_message_id: str | None = None,
) -> float:
    """SentinelZero task-success scoring with partial credit.

    Scoring scale:
      - Correct decision + correct action + correct target:  1.0
      - Correct decision + correct action + wrong/no target: 0.80
      - Correct decision + wrong action:                     0.55
      - Similar/adjacent decision + correct action:          partial (SIMILAR_PAIR_PARTIAL)
      - Wrong decision (but not an ALLOW-on-serious miss):   partial
      - ALLOW when truth is QUARANTINE or ESCALATE:          0.0 (safety-critical failure)
      - Invalid / missing decision:                           0.0
    """
    sub_res = _normalize_resolution(submitted_resolution)
    exp_res = _normalize_resolution(expected_resolution)

    # Guard: invalid decision → zero
    if sub_res not in VALID_DECISIONS:
        return 0.0

    # Guard: missing ground truth → fallback to decision match only
    if not exp_res or exp_res not in VALID_DECISIONS:
        return 0.5 if sub_res in VALID_DECISIONS else 0.0

    # Safety-critical miss: ALLOW when agent should have blocked
    if sub_res == "allow" and exp_res in ("quarantine", "escalate"):
        return 0.0

    # Perfect decision match
    if sub_res == exp_res:
        exp_tool = expected_action_tool or DECISION_TO_ACTION.get(exp_res, "")
        sub_tool = submitted_action_tool or ""

        if sub_tool == exp_tool:
            # Check message_id targeting
            if expected_message_id and submitted_message_id:
                if submitted_message_id == expected_message_id:
                    return 1.0
                else:
                    return 0.80  # correct action, wrong target
            return 1.0  # correct action, no target info to verify
        else:
            return 0.55  # correct decision, wrong/missing action

    # Similar adjacent pair partial credit
    pair = frozenset({sub_res, exp_res})
    if pair in SIMILAR_PAIR_PARTIAL:
        return SIMILAR_PAIR_PARTIAL[pair]

    # WARN when truth is ALLOW – being too cautious, not dangerous
    if sub_res == "warn" and exp_res == "allow":
        return 0.30

    # QUARANTINE when truth is ESCALATE (over-contained compromised account)
    if sub_res == "quarantine" and exp_res == "escalate":
        return SIMILAR_PAIR_PARTIAL.get(frozenset({"quarantine", "escalate"}), 0.40)

    # ESCALATE when truth is QUARANTINE (over-escalated simple phishing)
    if sub_res == "escalate" and exp_res == "quarantine":
        return 0.35

    return 0.0


# ─────────────────────────────────────────────────────────────────────────────
# 2. Policy (15%)
# ─────────────────────────────────────────────────────────────────────────────


def sz_score_policy(
    enforcement_rejections_count: int,
    submitted_resolution: str | None,
    expected_resolution: str | None,
    action_matches_decision: bool,
) -> float:
    """SentinelZero policy compliance score.

    Checks:
    1. Tool enforcement compliance: 25% penalty per rejection
    2. Decision-action pairing: submitted decision must map to submitted action
    3. Decision-ground-truth alignment: appropriate for the case
    """
    sub_res = _normalize_resolution(submitted_resolution)
    exp_res = _normalize_resolution(expected_resolution)

    # 1. Enforcement compliance: 25% deduction per policy rejection
    c_enforcement = max(0.0, 1.0 - 0.25 * float(enforcement_rejections_count))

    # 2. Decision-action coherence (did submitted action match submitted decision?)
    c_action_coherence = 1.0 if action_matches_decision else 0.5

    # 3. Policy alignment: penalize ALLOW on serious cases, reward good decisions
    c_alignment = 1.0
    if sub_res == "allow" and exp_res in ("quarantine", "escalate"):
        c_alignment = 0.0   # Critical policy violation
    elif sub_res == "allow" and exp_res == "warn":
        c_alignment = 0.4   # Missed a warning signal
    elif sub_res in VALID_DECISIONS and exp_res in VALID_DECISIONS and sub_res == exp_res:
        c_alignment = 1.0   # Correct policy choice
    elif frozenset({sub_res, exp_res}) in SIMILAR_PAIR_PARTIAL:
        c_alignment = 0.6   # Adjacent decision
    elif sub_res not in VALID_DECISIONS:
        c_alignment = 0.0

    return _clamp(c_enforcement * c_action_coherence * c_alignment)


# ─────────────────────────────────────────────────────────────────────────────
# 3. Evidence (15%)
# ─────────────────────────────────────────────────────────────────────────────


def sz_score_evidence(
    submitted_evidence: list[str],
    required_evidence: list[str],
    observed_evidence: set[str],
) -> tuple[float, float, float, float]:
    """SentinelZero evidence scoring using F1 (precision × recall harmonic mean).

    Validity check: submitted IDs must have actually appeared in tool responses
    (observed_evidence). Fabricated IDs count as false positives.

    Returns: (f1, precision, recall, true_positives)
    """
    r_set = set(required_evidence)
    e_sub = set(submitted_evidence)

    # Valid = cited AND actually returned by a tool
    e_valid = e_sub.intersection(observed_evidence)

    # True positives: valid AND required
    tp_set = e_valid.intersection(r_set)
    tp = float(len(tp_set))

    if len(e_sub) == 0:
        precision = 1.0 if len(r_set) == 0 else 0.0
    else:
        precision = tp / float(len(e_sub))

    if len(r_set) == 0:
        recall = 1.0
    else:
        recall = tp / float(len(r_set))

    if (precision + recall) == 0.0:
        f1 = 0.0
    else:
        f1 = (2.0 * precision * recall) / (precision + recall)

    return float(f1), float(precision), float(recall), tp


# ─────────────────────────────────────────────────────────────────────────────
# 4. Calibration (10%)
# ─────────────────────────────────────────────────────────────────────────────


def sz_score_calibration(
    confidence: Any,
    task_success: float,
    must_escalate: bool,
    escalation_required: bool,
) -> float:
    """SentinelZero calibration scoring.

    Rewards:
    - High confidence when decision is correct
    - Lower (but >0.5) confidence for genuinely ambiguous / hard cases
    - Penalizes extreme confidence (≥0.95) on incorrect decisions

    Prevents gaming with always-0.5: if confidence == 0.5 and task_success == 1.0,
    score is only 0.5 (half credit). Definitive correct high-confidence is rewarded.

    Also checks escalation flag matches must_escalate.
    """
    # Validate confidence
    if confidence is None or not isinstance(confidence, (int, float)):
        return 0.0
    conf = float(confidence)
    if conf < 0.0 or conf > 1.0:
        return 0.0

    # Escalation flag check
    m_escalate = 1.0
    if must_escalate and not escalation_required:
        m_escalate = 0.0
    elif not must_escalate and escalation_required:
        m_escalate = 0.5   # over-flagged escalation; partial penalty

    # Confidence alignment
    if task_success == 1.0:
        c_align = conf   # More confident on correct = better
    else:
        # Penalize high confidence on incorrect: extreme confidence is worst
        c_align = 1.0 - conf

    return _clamp(m_escalate * c_align)


# ─────────────────────────────────────────────────────────────────────────────
# 5. Efficiency (10%)
# ─────────────────────────────────────────────────────────────────────────────


def sz_score_efficiency(
    tool_calls_count: int,
    duplicate_calls_count: int,
    budget: int,
    task_success: float,
) -> float:
    """SentinelZero efficiency scoring.

    The expected beginner-friendly investigation uses 4–6 tool calls.
    Budget default is 10. Exceeding budget → 0. Duplicates penalize linearly.

    E_budget × (1 − P_loop)
    """
    budget_val = max(1, budget)

    if tool_calls_count == 0:
        # No tools called: can only be efficient if trivially correct (rare)
        e_budget = 0.5 if task_success == 1.0 else 0.0
    elif tool_calls_count > budget_val:
        e_budget = 0.0
    elif tool_calls_count <= 6:
        # Sweet spot: ≤6 calls is exactly what we ask beginner agents to do
        e_budget = 1.0
    else:
        # Gentle linear decay from 6..budget_val
        overage = tool_calls_count - 6
        range_above = max(1, budget_val - 6)
        e_budget = max(0.0, 1.0 - float(overage) / float(range_above))

    p_loop = float(duplicate_calls_count) / float(tool_calls_count) if tool_calls_count > 0 else 0.0
    p_loop = _clamp(p_loop)

    return _clamp(e_budget * (1.0 - p_loop))


# ─────────────────────────────────────────────────────────────────────────────
# 6. Communication (10%)
# ─────────────────────────────────────────────────────────────────────────────

# SentinelZero domain keywords for communication grounding check
_SZ_COMM_KEYWORDS = [
    "domain", "phishing", "quarantine", "allowed", "warn", "escalat",
    "spoofing", "credential", "impersonation", "social engineering",
    "prompt injection", "malicious", "suspicious", "sender", "evidence",
    "mfa", "password", "gift card", "wire transfer", "bank", "lookalike",
    "compromised", "legitimate", "identity", "verified", "unverified",
    "spf", "dkim", "dmarc", "threat", "attack", "policy", "security",
]

_SZ_DECISION_KEYWORDS: dict[str, list[str]] = {
    "allow": ["allow", "delivered", "legitimate", "verified", "trusted", "safe"],
    "warn": ["warn", "warning", "external", "caution", "banner", "review", "suspicious"],
    "quarantine": ["quarantine", "block", "blocked", "malicious", "phish", "scam", "harmful", "remove"],
    "escalate": ["escalat", "tier-2", "soc", "investigat", "compromised", "human review", "analyst"],
}


def sz_score_communication(
    summary: str | None,
    submitted_resolution: str | None,
    expected_resolution: str | None,
    task_success: float,
) -> float:
    """SentinelZero communication quality scoring (4-point rubric).

    1. Structure & Length (20–3000 chars):         0.25
    2. Domain grounding (cyber/SZ keywords):        0.25
    3. No false action claims (no lying about decisions taken): 0.25
    4. Decision consistency with resolution:        0.25
    """
    resp = (summary or "").strip()
    lower_resp = resp.lower()
    sub_res = _normalize_resolution(submitted_resolution)
    exp_res = _normalize_resolution(expected_resolution)
    score = 0.0

    # 1. Structure & Length
    if 20 <= len(resp) <= 3000:
        score += 0.25

    # 2. Domain Grounding
    has_domain_context = any(k in lower_resp for k in _SZ_COMM_KEYWORDS) or bool(
        SZ_EVIDENCE_PATTERN.search(resp)
    )
    if has_domain_context:
        score += 0.25

    # 3. No False Claims
    # Penalize if the summary says an action that wasn't actually taken
    false_claim = False
    if sub_res != "quarantine" and re.search(r"\bquarantine[d]?\b", lower_resp):
        if not re.search(r"\b(not\s+quarantine|without\s+quarantine|no\s+quarantine)\b", lower_resp):
            false_claim = True
    if sub_res != "escalate" and re.search(r"\bescalat(e[d]?|ing)\b", lower_resp):
        if "escalate" not in (sub_res or ""):
            false_claim = True

    if not false_claim:
        score += 0.25

    # 4. Decision Consistency
    consistent = False
    if sub_res in _SZ_DECISION_KEYWORDS:
        for kw in _SZ_DECISION_KEYWORDS[sub_res]:
            if kw in lower_resp:
                consistent = True
                break

    if consistent:
        score += 0.25

    return _clamp(score)


# ─────────────────────────────────────────────────────────────────────────────
# 7. Robustness (5%)
# ─────────────────────────────────────────────────────────────────────────────


def sz_score_robustness(
    task_success: float,
    crashed: bool = False,
    invalid_confidence: bool = False,
    invalid_decision: bool = False,
    fabricated_evidence_count: int = 0,
    multiple_actions_taken: int = 0,
) -> float:
    """SentinelZero robustness scoring.

    Penalizes:
    - crashes / exceptions
    - invalid confidence values
    - invalid decisions
    - fabricated evidence items
    - multiple contradictory actions

    Base: task_success. Deductions reduce it further.
    """
    base = task_success

    if crashed:
        return 0.0
    if invalid_decision:
        return 0.0

    penalties = 0.0
    if invalid_confidence:
        penalties += 0.30
    if fabricated_evidence_count > 0:
        penalties += min(0.20 * fabricated_evidence_count, 0.40)
    if multiple_actions_taken > 1:
        penalties += min(0.20 * (multiple_actions_taken - 1), 0.40)

    return _clamp(base - penalties)
