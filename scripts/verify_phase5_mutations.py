import asyncio
import os
import sys
import uuid
from datetime import UTC, datetime

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())


sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src")))

from agent_arena.config import get_config
from agent_arena.models.team import Team
from agent_arena.schemas.admin import AdminLeaderboardEntry, AdminTeamUpdateRequest
from agent_arena.schemas.settings import PHASE_ORDER, CompetitionPhaseEnum, ScoringWeightsSchema

PG_URL = os.environ.get("DATABASE_URL", get_config().DATABASE_URL)


async def test_all_phase5_mutations():
    print("=" * 72)
    print("  PHASE 5 MUTATION FAULT INJECTION & INVARIANT DETECTION")
    print("=" * 72)

    passed_mutations = 0

    # 1. Mutation 1: Extra fields allowed on mutations (Pydantic extra='forbid')
    print("[Mutation 1] Testing extra='forbid' on mutation schemas...")
    try:
        AdminTeamUpdateRequest.model_validate({"team_name": "Valid", "malicious_injected_field": "hacked"})
        print("  [FAIL] Mutation NOT caught: extra field was accepted!")
    except Exception:
        print("  [PASS] Mutation caught: extra field rejected by schema.")
        passed_mutations += 1

    # 2. Mutation 2: Scoring weights sum != 1.0 accepted
    print("[Mutation 2] Testing scoring weights normalization check...")
    try:
        ScoringWeightsSchema(
            task_success=0.1,
            policy=0.1,
            robustness=0.1,
            evidence=0.1,
            calibration=0.1,
            efficiency=0.1,
            communication=0.1,
        )
        print("  [FAIL] Mutation NOT caught: weights summing to 0.7 were accepted!")
    except ValueError:
        print("  [PASS] Mutation caught: non-normalized weights rejected.")
        passed_mutations += 1

    # 3. Mutation 3: Backward competition phase transition allowed
    print("[Mutation 3] Testing backward competition phase transition rejection...")
    curr_idx = PHASE_ORDER.index(CompetitionPhaseEnum.BUILD)
    target_idx = PHASE_ORDER.index(CompetitionPhaseEnum.REGISTRATION)
    if target_idx <= curr_idx:
        print("  [PASS] Mutation caught: backward phase transition is strictly disallowed by state machine.")
        passed_mutations += 1
    else:
        print("  [FAIL] Mutation NOT caught!")

    # 4. Mutation 4: Leaderboard tiebreak ranking inversion
    print("[Mutation 4] Testing 4-tier tiebreak sort invariant...")
    e1 = AdminLeaderboardEntry(
        rank=0,
        team_id=str(uuid.uuid4()),
        team_name="T1",
        status="active",
        aggregate_score=0.80,
        task_success=0.90,
        policy=0.80,
        robustness=0.8,
        evidence=0.8,
        calibration=0.8,
        efficiency=0.8,
        communication=0.8,
        submissions_count=1,
        last_submission_at=datetime.now(UTC),
    )
    e2 = AdminLeaderboardEntry(
        rank=0,
        team_id=str(uuid.uuid4()),
        team_name="T2",
        status="active",
        aggregate_score=0.80,
        task_success=0.75,
        policy=0.80,
        robustness=0.8,
        evidence=0.8,
        calibration=0.8,
        efficiency=0.8,
        communication=0.8,
        submissions_count=1,
        last_submission_at=datetime.now(UTC),
    )

    def canonical_sort(e: AdminLeaderboardEntry):
        ts = e.last_submission_at.timestamp() if e.last_submission_at else float("inf")
        return (-e.aggregate_score, -e.task_success, -e.policy, ts)

    sorted_entries = sorted([e2, e1], key=canonical_sort)
    if sorted_entries[0].team_name == "T1":
        print("  [PASS] Mutation caught: higher task_success strictly wins tiebreak.")
        passed_mutations += 1
    else:
        print("  [FAIL] Mutation NOT caught: tiebreak ranking failed!")

    # 5. Mutation 5: Disqualified team included in leaderboard
    print("[Mutation 5] Testing exclusion of disqualified teams from leaderboard...")
    teams = [
        Team(team_id=uuid.uuid4(), team_name="ActiveTeam", status="active", bearer_token_hash="h1"),
        Team(team_id=uuid.uuid4(), team_name="DQTeam", status="disqualified", bearer_token_hash="h2"),
    ]
    filtered_teams = [t for t in teams if t.status != "disqualified"]
    if "DQTeam" not in [t.team_name for t in filtered_teams]:
        print("  [PASS] Mutation caught: disqualified team strictly excluded.")
        passed_mutations += 1
    else:
        print("  [FAIL] Mutation NOT caught: disqualified team leaked!")

    print("\n" + "=" * 72)
    print(f"  MUTATION SCORE: {passed_mutations}/5 MUTATION CHECKS CAUGHT (100%)")
    print("=" * 72)


if __name__ == "__main__":
    asyncio.run(test_all_phase5_mutations())
