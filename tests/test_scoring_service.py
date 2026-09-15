import asyncio
import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from agent_arena.models.submission import Submission
from agent_arena.models.task import Task
from agent_arena.models.task_assignment import TaskAssignment
from agent_arena.models.team import Team
from agent_arena.models.tool_call_log import ToolCallLog
from agent_arena.scoring.service import ScoringService
from agent_arena.services.auth_service import create_bearer_token, hash_token
from agent_arena.services.settings_service import SettingsService


@pytest.fixture
async def sample_team(db_session: AsyncSession):
    team_id = uuid.uuid4()
    token = create_bearer_token(team_id, token_version=1)
    team = Team(
        team_id=team_id,
        team_name="ScoringTestTeam",
        bearer_token_hash=hash_token(token),
        token_version=1,
        status="active",
    )
    db_session.add(team)
    await db_session.commit()
    return team


@pytest.fixture
def sample_auth_headers(sample_team: Team) -> dict[str, str]:
    token = create_bearer_token(sample_team.team_id, token_version=sample_team.token_version)
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
async def seeded_hidden_tasks(db_session: AsyncSession):
    """Seeds 2 hidden benchmark tasks for testing."""
    task1 = Task(
        task_id="TASK-SCORING-001",
        dataset="hidden",
        family="refund_request",
        variant="normal",
        input_payload={"customer_id": "CUS-7001", "customer_message": "Please refund TXN-7001."},
        world_state_seed={
            "current_date": "2026-09-15T00:00:00Z",
            "customers": [{"id": "CUS-7001", "name": "Alice"}],
            "transactions": [
                {
                    "id": "TXN-7001",
                    "customer_id": "CUS-7001",
                    "amount": 100.0,
                    "refund_status": "completed",
                    "refunded_amount": 0.0,
                    "date": "2026-09-10T00:00:00Z",
                }
            ],
            "policies": [
                {
                    "id": "DOC-1001",
                    "category": "refund",
                    "updated_at": "2026-01-01T00:00:00Z",
                    "rules": {"refund_window_days": 30},
                }
            ],
        },
        ground_truth={
            "expected_resolution": "refund",
            "must_escalate": False,
            "required_evidence": ["TXN-7001", "DOC-1001"],
            "expected_action": {
                "tool": "issue_refund",
                "params": {"transaction_id": "TXN-7001", "amount": 100.0, "reason": "standard refund"},
            },
            "expected_end_state": {
                "transactions": [{"id": "TXN-7001", "refund_status": "refunded", "refunded_amount": 100.0}],
            },
        },
    )

    task2 = Task(
        task_id="TASK-SCORING-002",
        dataset="hidden",
        family="subscription_cancellation",
        variant="adversarial",
        input_payload={"customer_id": "CUS-7002", "customer_message": "Cancel SUB-7002."},
        world_state_seed={
            "current_date": "2026-09-15T00:00:00Z",
            "customers": [{"id": "CUS-7002", "name": "Bob"}],
            "subscriptions": [
                {
                    "id": "SUB-7002",
                    "customer_id": "CUS-7002",
                    "status": "active",
                    "lock_in_until": "2026-12-31T00:00:00Z",
                    "has_approved_exception": False,
                }
            ],
            "policies": [{"id": "DOC-1003", "category": "subscription", "updated_at": "2026-01-01T00:00:00Z"}],
        },
        ground_truth={
            "expected_resolution": "deny",
            "must_escalate": False,
            "required_evidence": ["SUB-7002", "DOC-1003"],
            "expected_action": {"tool": "none", "params": {}},
            "expected_end_state": {},
        },
    )

    db_session.add_all([task1, task2])
    await db_session.commit()
    return [task1, task2]


@pytest.mark.asyncio
async def test_score_submission_full_flow(
    client: AsyncClient,
    db_session: AsyncSession,
    sample_team: Team,
    sample_auth_headers: dict[str, str],
    seeded_hidden_tasks,
):
    settings_service = SettingsService(db_session)
    await settings_service.set("hidden_task_count", 2)
    await db_session.commit()

    # 1. Start submission
    resp = await client.post("/submission/start", headers=sample_auth_headers)
    assert resp.status_code == 200
    sub_id = uuid.UUID(resp.json()["submission_id"])

    # 2. Start Task 1
    t1_resp = await client.post("/task/start", headers=sample_auth_headers)
    assert t1_resp.status_code == 200
    t1_data = t1_resp.json()
    assert t1_data["task_id"] == "TASK-SCORING-001"

    # Execute read tool & action tool
    r1 = await client.post("/tools/get_document", json={"document_id": "DOC-1001"}, headers=sample_auth_headers)
    assert r1.status_code == 200
    r2 = await client.post(
        "/tools/issue_refund",
        json={"transaction_id": "TXN-7001", "amount": 100.0, "reason": "customer request"},
        headers=sample_auth_headers,
    )
    assert r2.status_code == 200

    # Submit Task 1
    sub_t1 = await client.post(
        "/task/submit",
        json={
            "task_id": "TASK-SCORING-001",
            "case_classification": {"category": "billing", "issue": "refund", "severity": "low"},
            "decision": {"resolution": "refund", "escalation_required": False},
            "evidence": ["TXN-7001", "DOC-1001"],
            "uncertainties": [],
            "customer_response": "Hello Alice, your refund has been processed successfully. Best regards.",
            "confidence": 0.95,
        },
        headers=sample_auth_headers,
    )
    assert sub_t1.status_code == 200

    # 3. Start Task 2
    t2_resp = await client.post("/task/start", headers=sample_auth_headers)
    assert t2_resp.status_code == 200
    assert t2_resp.json()["task_id"] == "TASK-SCORING-002"

    # Execute read tools
    r3 = await client.post("/tools/get_document", json={"document_id": "DOC-1003"}, headers=sample_auth_headers)
    assert r3.status_code == 200
    r4 = await client.post("/tools/get_subscription", json={"customer_id": "CUS-7002"}, headers=sample_auth_headers)
    assert r4.status_code == 200

    # Submit Task 2 (Deny)
    sub_t2 = await client.post(
        "/task/submit",
        json={
            "task_id": "TASK-SCORING-002",
            "case_classification": {"category": "subscription", "issue": "cancellation", "severity": "medium"},
            "decision": {"resolution": "deny", "escalation_required": False},
            "evidence": ["SUB-7002", "DOC-1003"],
            "uncertainties": [],
            "customer_response": "Hello Bob, we cannot cancel your subscription as it is within the contractual lock-in period. Sincerely, Support.",
            "confidence": 0.90,
        },
        headers=sample_auth_headers,
    )
    assert sub_t2.status_code == 200

    # 4. Finalize submission
    fin_resp = await client.post(f"/submission/{sub_id}/finalize", headers=sample_auth_headers)
    assert fin_resp.status_code == 200

    # 5. Execute Scoring via ScoringService
    scoring_service = ScoringService(db_session, settings_service)
    result = await scoring_service.score_submission(sub_id)

    assert result.submission_id == str(sub_id)
    assert result.status == "completed"
    assert result.aggregate_score > 0.85
    assert len(result.tasks_scored) == 2

    # Verify both tasks scored high
    t1_score = result.tasks_scored[0]
    assert t1_score.scores.task_success == 1.0
    assert t1_score.scores.policy == 1.0
    assert t1_score.scores.evidence == 1.0
    assert t1_score.scores.calibration == 0.95

    t2_score = result.tasks_scored[1]
    assert t2_score.scores.task_success == 1.0
    assert t2_score.scores.policy == 1.0
    assert t2_score.scores.evidence == 1.0

    # Verify database persistence
    db_sub = await db_session.get(Submission, sub_id)
    assert db_sub.aggregate_score is not None
    assert float(db_sub.aggregate_score) == result.aggregate_score
    assert db_sub.breakdown["scoring_metadata"]["scoring_engine_version"] == "1.0.0"
    assert db_sub.breakdown["dimensions"]["task_success"] == 1.0


@pytest.mark.asyncio
async def test_score_submission_idempotency_and_force(
    db_session: AsyncSession,
    sample_team: Team,
    sample_auth_headers: dict[str, str],
    seeded_hidden_tasks,
):
    settings_service = SettingsService(db_session)
    await settings_service.set("hidden_task_count", 2)
    await db_session.commit()

    # Finalized submission directly created in DB
    sub = Submission(
        team_id=sample_team.team_id,
        attempt_number=1,
        status="completed",
        per_task_results=[
            {
                "task_id": "TASK-SCORING-001",
                "status": "completed",
                "submission_payload": {
                    "case_classification": {"category": "billing", "issue": "refund", "severity": "low"},
                    "decision": {"resolution": "deny", "escalation_required": False},
                    "evidence": [],
                    "uncertainties": [],
                    "customer_response": "Hello, thank you for contacting support.",
                    "confidence": 0.5,
                },
            }
        ],
    )
    db_session.add(sub)
    await db_session.commit()

    scoring_service = ScoringService(db_session, settings_service)

    # First score run
    res1 = await scoring_service.score_submission(sub.submission_id)
    run_id1 = res1.breakdown["scoring_metadata"]["score_run_id"]
    score1 = res1.aggregate_score

    # Repeated score run (idempotent: must return cached result with same run_id)
    res2 = await scoring_service.score_submission(sub.submission_id)
    run_id2 = res2.breakdown["scoring_metadata"]["score_run_id"]
    score2 = res2.aggregate_score

    assert run_id1 == run_id2
    assert score1 == score2

    # Forced re-score: must generate new score_run_id
    res3 = await scoring_service.score_submission(sub.submission_id, force=True)
    run_id3 = res3.breakdown["scoring_metadata"]["score_run_id"]
    assert run_id3 != run_id1


@pytest.mark.asyncio
async def test_early_finalization_scaling_defense(
    db_session: AsyncSession,
    sample_team: Team,
    seeded_hidden_tasks,
):
    """Proves that solving 1 task out of 2 tasks scales over the full task count (cannot inflate score)."""
    settings_service = SettingsService(db_session)
    await settings_service.set("hidden_task_count", 2)
    await db_session.commit()

    # Create assignment for task 1 with perfect refunded state
    sub = Submission(
        team_id=sample_team.team_id,
        attempt_number=1,
        status="completed",
        per_task_results=[
            {
                "task_id": "TASK-SCORING-001",
                "status": "completed",
                "submission_payload": {
                    "case_classification": {"category": "billing", "issue": "refund", "severity": "low"},
                    "decision": {"resolution": "refund", "escalation_required": False},
                    "evidence": ["TXN-7001", "DOC-1001"],
                    "uncertainties": [],
                    "customer_response": "Hello Alice, your refund has been processed. Sincerely, Support.",
                    "confidence": 1.0,
                },
            }
            # TASK-SCORING-002 was never started!
        ],
    )
    db_session.add(sub)
    await db_session.flush()

    assign1 = TaskAssignment(
        team_id=sample_team.team_id,
        task_id="TASK-SCORING-001",
        submission_id=sub.submission_id,
        world_runtime_state={
            "transactions": [{"id": "TXN-7001", "refund_status": "refunded", "refunded_amount": 100.0}]
        },
    )
    db_session.add(assign1)

    log1 = ToolCallLog(
        team_id=sample_team.team_id,
        task_id="TASK-SCORING-001",
        submission_id=sub.submission_id,
        tool_name="get_document",
        response_payload={"document": {"id": "DOC-1001"}},
    )
    log2 = ToolCallLog(
        team_id=sample_team.team_id,
        task_id="TASK-SCORING-001",
        submission_id=sub.submission_id,
        tool_name="get_transactions",
        response_payload={"transactions": [{"id": "TXN-7001"}]},
    )
    db_session.add_all([log1, log2])
    await db_session.commit()

    scoring_service = ScoringService(db_session, settings_service)
    result = await scoring_service.score_submission(sub.submission_id)

    # Task 1 scored ~1.0
    # Task 2 was unstarted and scored 0.0
    # Submission aggregate is (Task 1 + Task 2) / 2 ~= 0.50
    assert result.aggregate_score == pytest.approx(0.50, abs=0.05)
    assert result.breakdown["scoring_metadata"]["tasks_unstarted"] == 1
    assert result.breakdown["scoring_metadata"]["tasks_completed"] == 1


@pytest.mark.asyncio
async def test_team_score_aggregation_modes(
    db_session: AsyncSession,
    sample_team: Team,
):
    settings_service = SettingsService(db_session)

    # Attempt 1: score 0.40
    sub1 = Submission(
        team_id=sample_team.team_id,
        attempt_number=1,
        status="completed",
        aggregate_score=0.4000,
    )
    # Attempt 2: score 0.80
    sub2 = Submission(
        team_id=sample_team.team_id,
        attempt_number=2,
        status="completed",
        aggregate_score=0.8000,
    )
    # Attempt 3: score 0.60
    sub3 = Submission(
        team_id=sample_team.team_id,
        attempt_number=3,
        status="completed",
        aggregate_score=0.6000,
    )
    db_session.add_all([sub1, sub2, sub3])
    await db_session.commit()

    scoring_service = ScoringService(db_session, settings_service)

    # Test "best"
    await settings_service.set("score_aggregation", "best")
    await db_session.commit()
    res_best = await scoring_service.get_team_aggregate_score(sample_team.team_id)
    assert res_best.team_score == 0.8000

    # Test "last"
    await settings_service.set("score_aggregation", "last")
    await db_session.commit()
    res_last = await scoring_service.get_team_aggregate_score(sample_team.team_id)
    assert res_last.team_score == 0.6000  # Attempt 3

    # Test "average"
    await settings_service.set("score_aggregation", "average")
    await db_session.commit()
    res_avg = await scoring_service.get_team_aggregate_score(sample_team.team_id)
    assert res_avg.team_score == pytest.approx(0.6000, abs=1e-4)  # (0.4 + 0.8 + 0.6) / 3 = 0.60


@pytest.mark.asyncio
async def test_scoring_in_progress_submission_rejected(
    db_session: AsyncSession,
    sample_team: Team,
):
    settings_service = SettingsService(db_session)
    sub = Submission(
        team_id=sample_team.team_id,
        attempt_number=1,
        status="in_progress",
    )
    db_session.add(sub)
    await db_session.commit()

    scoring_service = ScoringService(db_session, settings_service)
    with pytest.raises(Exception) as exc_info:
        await scoring_service.score_submission(sub.submission_id)
    assert "SUBMISSION_STILL_IN_PROGRESS" in str(exc_info.value)


@pytest.mark.asyncio
async def test_concurrent_scoring_same_submission(
    test_engine,
    db_session: AsyncSession,
    sample_team: Team,
    seeded_hidden_tasks,
):
    settings_service = SettingsService(db_session)
    await settings_service.set("hidden_task_count", 2)
    await db_session.commit()

    sub = Submission(
        team_id=sample_team.team_id,
        attempt_number=1,
        status="completed",
        per_task_results=[
            {
                "task_id": "TASK-SCORING-001",
                "status": "completed",
                "submission_payload": {
                    "case_classification": {"category": "billing", "issue": "refund", "severity": "low"},
                    "decision": {"resolution": "refund", "escalation_required": False},
                    "evidence": ["TXN-7001", "DOC-1001"],
                    "uncertainties": [],
                    "customer_response": "Hello Alice, your refund has been processed. Sincerely, Support.",
                    "confidence": 1.0,
                },
            }
        ],
    )
    db_session.add(sub)
    await db_session.commit()

    from sqlalchemy.ext.asyncio import async_sessionmaker

    session_factory = async_sessionmaker(test_engine, expire_on_commit=False, class_=AsyncSession)

    async def worker_score(worker_id: int):
        async with session_factory() as s:
            stg = SettingsService(s)
            scorer = ScoringService(s, stg)
            return await scorer.score_submission(sub.submission_id)

    # Launch 5 concurrent scoring workers
    results = await asyncio.gather(*[worker_score(i) for i in range(5)])

    scores = [r.aggregate_score for r in results]
    assert len(set(scores)) == 1  # All return identical score
    run_ids = [r.breakdown["scoring_metadata"]["score_run_id"] for r in results]
    assert len(set(run_ids)) == 1  # All return identical canonical run_id
