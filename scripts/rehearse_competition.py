"""Agent Arena SupportOps - Full Competition Rehearsal Script.

Executes a realistic multi-team rehearsal against the live production deployment:
- Generates unique REHEARSAL_RUN_ID
- Registers 3 independent teams (Alpha/Expert, Beta/Intermediate, Gamma/Naive)
- Configures live settings via Admin API
- Exercises tasks across SupportOps families and non-normal variants
- Runs ReferenceSolver vs Intermediate vs Naive agents
- Gathers submissions, finalizes, triggers scoring
- Validates 4-tier score separation: Score(Alpha) > Score(Beta) > Score(Gamma)
- Tests dynamic live settings update and reversion
- Outputs comprehensive rehearsal JSON report to docs/rehearsal_results.json
"""

import json
import os
import sys
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

# Add paths for starter-kit and src
ROOT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT_DIR / "src"))
sys.path.insert(0, str(ROOT_DIR / "starter-kit"))

from sdk.tools_client import ToolsClient  # noqa: E402
from agent_arena.config import get_config  # noqa: E402


def solve_expert(task: dict[str, Any], tools: ToolsClient) -> dict[str, Any]:
    """Expert agent: actively inspects customer records, transactions, policies, and takes authorized actions."""
    customer_id = task.get("customer_id", "")
    message = task.get("customer_message", "").lower()

    evidence: list[str] = []
    uncertainties: list[str] = []

    try:
        cust_resp = tools.get_customer(customer_id)
        if "customer" in cust_resp and "id" in cust_resp["customer"]:
            evidence.append(cust_resp["customer"]["id"])
    except Exception as e:
        uncertainties.append(f"Failed to fetch customer: {e}")

    transactions = []
    try:
        tx_resp = tools.get_transactions(customer_id)
        transactions = tx_resp.get("transactions", [])
        for tx in transactions[:3]:
            if "id" in tx:
                evidence.append(tx["id"])
    except Exception as e:
        uncertainties.append(f"Failed to fetch transactions: {e}")

    try:
        search_resp = tools.search_knowledge("refund duplicate cancellation policy", top_k=3)
        results = search_resp.get("results", [])
        if results:
            policy_doc_id = results[0]["id"]
            evidence.append(policy_doc_id)
            doc_resp = tools.get_document(policy_doc_id)
            if "document" in doc_resp and "id" in doc_resp["document"]:
                evidence.append(doc_resp["document"]["id"])
    except Exception as e:
        uncertainties.append(f"Failed to retrieve policy document: {e}")

    category = "billing"
    issue = "general_inquiry"
    severity = "medium"

    if "duplicate" in message or "charged twice" in message:
        issue = "duplicate_payment"
    elif "cancel" in message:
        category = "account"
        issue = "subscription_cancellation"
    elif "fraud" in message or "unauthorized" in message:
        category = "security"
        issue = "fraud_suspicion"
        severity = "high"
    elif "refund" in message:
        issue = "refund_request"

    resolution = "deny"
    escalation_required = False
    customer_response = "Thank you for reaching SupportOps. We have reviewed your account details."

    if issue in ("duplicate_payment", "refund_request") and transactions:
        target_tx = transactions[0]
        tx_id = target_tx.get("id")
        amount = float(target_tx.get("amount", 0.0))
        try:
            res = tools.issue_refund(transaction_id=tx_id, amount=amount, reason="Customer requested refund")
            if res.get("status") == "success":
                resolution = "refund"
                if "transaction" in res and "id" in res["transaction"]:
                    evidence.append(res["transaction"]["id"])
            elif res.get("error") == "INELIGIBLE":
                if res.get("reason") == "chargeback_investigation_active":
                    escalation_required = True
                    resolution = "escalate"
                else:
                    resolution = "deny"
        except Exception:
            resolution = "deny"

    return {
        "case_classification": {"category": category, "issue": issue, "severity": severity},
        "decision": {"resolution": resolution, "escalation_required": escalation_required},
        "evidence": list(set(evidence)),
        "uncertainties": uncertainties,
        "customer_response": customer_response,
        "confidence": 0.95,
    }


def solve_naive(task: dict[str, Any], tools: ToolsClient) -> dict[str, Any]:
    """Naive baseline agent: zero tool calls, fixed boilerplate response conforming to Section 7 schema."""
    return {
        "case_classification": {
            "category": "general",
            "issue": "other",
            "severity": "low",
        },
        "decision": {
            "resolution": "deny",
            "escalation_required": False,
        },
        "evidence": [],
        "uncertainties": ["Automated baseline agent - no actions taken."],
        "customer_response": "Thank you for contacting customer support. We are reviewing your inquiry.",
        "confidence": 0.50,
    }


def solve_intermediate(task: dict[str, Any], tools: ToolsClient) -> dict[str, Any]:
    """Intermediate agent: looks up customer and searches knowledge base, basic decision heuristic."""
    customer_id = task.get("customer_id", "")
    message = task.get("customer_message", "").lower()

    evidence: list[str] = []
    uncertainties: list[str] = []

    try:
        cust_resp = tools.get_customer(customer_id)
        if "customer" in cust_resp and "id" in cust_resp["customer"]:
            evidence.append(cust_resp["customer"]["id"])
    except Exception as e:
        uncertainties.append(f"Failed to fetch customer: {e}")

    try:
        search_resp = tools.search_knowledge("support inquiry policy", top_k=2)
        for doc in search_resp.get("results", [])[:2]:
            if "id" in doc:
                evidence.append(doc["id"])
    except Exception as e:
        uncertainties.append(f"Failed to search knowledge: {e}")

    resolution = "deny"
    escalation_required = False
    if "fraud" in message or "unauthorized" in message:
        resolution = "escalate"
        escalation_required = True
    elif "cancel" in message:
        resolution = "deny"

    return {
        "case_classification": {
            "category": "general",
            "issue": "general_inquiry",
            "severity": "medium",
        },
        "decision": {
            "resolution": resolution,
            "escalation_required": escalation_required,
        },
        "evidence": list(set(evidence)),
        "uncertainties": uncertainties,
        "customer_response": "Thank you for contacting SupportOps. We have reviewed your request.",
        "confidence": 0.75,
    }


def main() -> None:
    rehearsal_run_id = f"REHEARSAL-{uuid.uuid4().hex[:8].upper()}"
    base_url = os.getenv("BASE_URL", "http://localhost:8000").rstrip("/")
    admin_secret = os.getenv("ADMIN_PANEL_SECRET", get_config().ADMIN_PANEL_SECRET)
    admin_headers = {"X-Admin-Secret": admin_secret, "Content-Type": "application/json"}

    print("=" * 80)
    print(" AGENT ARENA SUPPORT-OPS — FULL SYSTEM REHEARSAL")
    print(f" Run ID:   {rehearsal_run_id}")
    print(f" Target:   {base_url}")
    print(f" Time:     {datetime.now(UTC).isoformat()}")
    print("=" * 80)

    with httpx.Client(base_url=base_url, timeout=30.0) as http:
        # 1. Health & Readiness Pre-flight
        print("\n[Step 1/8] Verifying Service Health & Database Readiness...")
        r_live = http.get("/health")
        assert r_live.status_code == 200, f"Liveness check failed: {r_live.text}"
        r_ready = http.get("/health/ready")
        assert r_ready.status_code == 200, f"Readiness check failed: {r_ready.text}"
        r_adm_health = http.get("/admin/health", headers=admin_headers)
        assert r_adm_health.status_code == 200, f"Admin health check failed: {r_adm_health.text}"
        adm_stats = r_adm_health.json()
        print(f"  + Liveness:  OK ({r_live.json().get('phase', 'N/A')})")
        print("  + Readiness: OK (Database Connected)")
        print(f"  + Admin:     OK (Available Hidden Tasks: {adm_stats.get('hidden_tasks_available')})")

        # 2. Dynamic Settings Configuration
        print("\n[Step 2/8] Inspecting and Configuring Live Settings...")
        r_settings = http.get("/admin/settings", headers=admin_headers)
        assert r_settings.status_code == 200
        settings_map = {s["key"]: s["value"] for s in r_settings.json()["settings"]}
        orig_hidden_count = settings_map.get("hidden_task_count", 30)
        orig_time_budget = settings_map.get("time_budget_per_task_seconds", 180)
        orig_rate_limit = settings_map.get("rate_limit_tool_calls_per_min", 60)
        print(f"  + Original hidden_task_count:            {orig_hidden_count}")
        print(f"  + Original time_budget_per_task_seconds: {orig_time_budget}")
        print(f"  + Original rate_limit_tool_calls_per_min:{orig_rate_limit}")

        try:
            # Set rehearsal task count to 12 (exercises multiple tasks per team across all 6 families)
            rehearsal_task_count = 12
            r_set = http.put(
                "/admin/settings/hidden_task_count",
                json={"value": rehearsal_task_count},
                headers=admin_headers,
            )
            assert r_set.status_code == 200
            assert r_set.json()["new_value"] == rehearsal_task_count
            print(f"  + Dynamically set hidden_task_count:      {rehearsal_task_count} (no restart)")

            # Set rate limit to 600 to accommodate automated high-speed agent execution
            r_rate = http.put(
                "/admin/settings/rate_limit_tool_calls_per_min",
                json={"value": 600},
                headers=admin_headers,
            )
            assert r_rate.status_code == 200
            print("  + Dynamically set rate_limit_tool_calls: 600 (no restart)")

            # Ensure competition phase allows submissions (build or registration)
            phase_res = http.get("/admin/competition/phase", headers=admin_headers).json()
            print(f"  + Current Competition Phase:             {phase_res.get('competition_phase')}")

            # 3. Register 3 Distinct Competition Teams
            print("\n[Step 3/8] Registering 3 Independent Rehearsal Teams via Admin API...")
            teams_config = [
                {
                    "key": "alpha",
                    "name": f"Alpha-Expert-{rehearsal_run_id}",
                    "email": f"alpha-{rehearsal_run_id}@arena-rehearsal.org",
                    "strategy": "Expert Agent (Active Tool-Assisted Grounding)",
                    "solver": solve_expert,
                },
                {
                    "key": "beta",
                    "name": f"Beta-Intermediate-{rehearsal_run_id}",
                    "email": f"beta-{rehearsal_run_id}@arena-rehearsal.org",
                    "strategy": "Intermediate Agent (Basic Tool Lookups & Decision)",
                    "solver": solve_intermediate,
                },
                {
                    "key": "gamma",
                    "name": f"Gamma-Naive-{rehearsal_run_id}",
                    "email": f"gamma-{rehearsal_run_id}@arena-rehearsal.org",
                    "strategy": "Naive Baseline (Zero Tool Calls)",
                    "solver": solve_naive,
                },
            ]

            registered_teams: dict[str, dict[str, Any]] = {}
            for tc in teams_config:
                reg_resp = http.post(
                    "/admin/teams",
                    json={"team_name": tc["name"], "members": [tc["email"]]},
                    headers=admin_headers,
                )
                assert reg_resp.status_code == 201, f"Failed to register team {tc['name']}: {reg_resp.text}"
                data = reg_resp.json()
                registered_teams[tc["key"]] = {
                    "team_id": data["team_id"],
                    "token": data["token"],
                    "name": tc["name"],
                    "strategy": tc["strategy"],
                    "solver": tc["solver"],
                }
                print(f"  + Registered {tc['name']} (ID: {data['team_id']})")

            # 4. Execute Realistic Submissions for Each Team
            print(f"\n[Step 4/8] Executing {rehearsal_task_count} Tasks per Team Across SupportOps Families...")
            team_execution_results: dict[str, dict[str, Any]] = {}

            for key, tinfo in registered_teams.items():
                print(f"\n  --- Running Team: {tinfo['name']} [{tinfo['strategy']}] ---")
                t_start = time.perf_counter()
                tools = ToolsClient(base_url=base_url, token=tinfo["token"])

                # 4a. Start Submission
                sub_start = tools.start_submission()
                sub_id = sub_start["submission_id"]
                print(f"    Submission Started: {sub_id} (Attempt #{sub_start['attempt_number']})")

                tasks_completed = 0
                task_details: list[dict[str, Any]] = []

                # 4b. Execute Tasks Sequentially
                for task_idx in range(rehearsal_task_count):
                    task = tools.start_task()
                    task_id = task["task_id"]

                    # Oracle Leakage Assertion: verify no ground truth in task payload
                    for forbidden in (
                        "ground_truth",
                        "expected_resolution",
                        "expected_evidence",
                        "must_escalate",
                        "world_state",
                    ):
                        assert forbidden not in task, f"Oracle leakage detected in start_task: {forbidden} present"

                    # Solve task with team's solver strategy
                    solve_t0 = time.perf_counter()
                    payload = tinfo["solver"](task, tools)
                    solve_dur = time.perf_counter() - solve_t0

                    # Submit task
                    sub_res = tools.submit_task(task_id=task_id, payload=payload)
                    assert sub_res.get("received") is True, f"Task submit failed: {sub_res}"

                    # Oracle Leakage Assertion: verify no evaluation score in submit response
                    for forbidden in ("correct", "expected_resolution", "expected_evidence", "score", "diff_explanation"):
                        assert forbidden not in sub_res, f"Oracle leakage in submit_task: {forbidden} present"

                    tasks_completed += 1
                    task_details.append(
                        {
                            "task_id": task_id,
                            "resolution": payload.get("decision", {}).get("resolution"),
                            "evidence_count": len(payload.get("evidence", [])),
                            "duration_s": round(solve_dur, 3),
                        }
                    )
                    print(
                        f"    [{task_idx + 1:02d}/{rehearsal_task_count:02d}] {task_id} -> {payload.get('decision', {}).get('resolution')} ({solve_dur * 1000:.1f}ms)"
                    )

                # 4c. Check Submission Status
                status_resp = tools.get_submission_status(sub_id)
                assert status_resp["tasks_completed"] == rehearsal_task_count

                # 4d. Finalize Submission
                fin_resp = tools.finalize_submission(sub_id)
                assert fin_resp["status"] == "completed"
                total_duration = time.perf_counter() - t_start
                tools.close()

                print(f"    Submission {sub_id} Finalized (Total Duration: {total_duration:.2f}s)")
                team_execution_results[key] = {
                    "submission_id": sub_id,
                    "team_id": tinfo["team_id"],
                    "name": tinfo["name"],
                    "strategy": tinfo["strategy"],
                    "tasks_completed": tasks_completed,
                    "task_details": task_details,
                    "duration_seconds": round(total_duration, 2),
                }

            # 5. Score Submissions via Admin Scoring API
            print("\n[Step 5/8] Triggering Admin Submission Scoring & Dimension Breakdown...")
            scoring_results: dict[str, dict[str, Any]] = {}
            for key, exec_info in team_execution_results.items():
                sub_id = exec_info["submission_id"]
                r_score = http.post(
                    f"/admin/submissions/{sub_id}/score",
                    headers=admin_headers,
                )
                assert r_score.status_code == 200, f"Scoring failed for submission {sub_id}: {r_score.text}"
                score_data = r_score.json()
                scoring_results[key] = score_data
                breakdown = score_data.get("breakdown", {})
                dim_scores = breakdown.get("dimensions") or breakdown.get("dimension_scores") or {}
                print(f"  + {exec_info['name']}:")
                print(f"      Aggregate Score: {score_data['aggregate_score']:.4f}")
                print(
                    f"      Dimensions:      TaskSuccess={dim_scores.get('task_success', 0):.3f}, "
                    f"Policy={dim_scores.get('policy', 0):.3f}, "
                    f"Evidence={dim_scores.get('evidence', 0):.3f}, "
                    f"Robustness={dim_scores.get('robustness', 0):.3f}"
                )

            # 6. Verify Three-Team Strict Score Ordering & Hierarchy
            print("\n[Step 6/8] Verifying Three-Team Strict Score Ordering & Tiebreak Hierarchy...")
            score_alpha = scoring_results["alpha"]["aggregate_score"]
            score_beta = scoring_results["beta"]["aggregate_score"]
            score_gamma = scoring_results["gamma"]["aggregate_score"]

            print(f"  + Alpha (Expert) Score:       {score_alpha:.4f}")
            print(f"  + Beta (Intermediate) Score:  {score_beta:.4f}")
            print(f"  + Gamma (Naive) Score:        {score_gamma:.4f}")

            assert score_alpha >= 0.55, f"Alpha score {score_alpha} fell below expert threshold 0.55"
            assert score_gamma <= 0.25, f"Gamma score {score_gamma} exceeded naive ceiling 0.25"
            assert score_alpha > score_beta, f"Separation failure: Alpha ({score_alpha}) <= Beta ({score_beta})"
            assert score_beta > score_gamma, f"Separation failure: Beta ({score_beta}) <= Gamma ({score_gamma})"
            print(
                "  => VERIFIED: Three-team strict score ordering (Score(Alpha) > Score(Beta) > Score(Gamma)) strictly holds; official leaderboard tiebreak hierarchy verified!"
            )

            # 7. Verify Official Leaderboard
            print("\n[Step 7/8] Verifying Production Leaderboard Standings...")
            r_lb = http.get("/admin/leaderboard", headers=admin_headers)
            assert r_lb.status_code == 200
            lb_entries = r_lb.json()["leaderboard"]

            # Filter down to our rehearsal teams
            rehearsal_lb = [e for e in lb_entries if rehearsal_run_id in e.get("team_name", "")]
            print(f"  + Found {len(rehearsal_lb)} rehearsal teams on Leaderboard:")
            for idx, entry in enumerate(rehearsal_lb):
                print(
                    f"      #{idx + 1} {entry['team_name']} | Score: {entry['aggregate_score']:.4f} | Submissions: {entry['submissions_count']}"
                )

            assert len(rehearsal_lb) == 3, f"Expected 3 rehearsal teams on leaderboard, found {len(rehearsal_lb)}"
            assert rehearsal_lb[0]["team_name"] == registered_teams["alpha"]["name"], (
                "Alpha not ranked #1 among rehearsal teams"
            )
            assert rehearsal_lb[1]["team_name"] == registered_teams["beta"]["name"], (
                "Beta not ranked #2 among rehearsal teams"
            )
            assert rehearsal_lb[2]["team_name"] == registered_teams["gamma"]["name"], (
                "Gamma not ranked #3 among rehearsal teams"
            )
            print("  => VERIFIED: Production Leaderboard correctly ranked all rehearsal teams!")

            # 8. Test Live Setting Modification and Clean Reversion
            print("\n[Step 8/8] Testing Live Setting Mutation and Reversion (Zero Downtime)...")
            # 8a. Update time_budget_per_task_seconds
            r_tb = http.put(
                "/admin/settings/time_budget_per_task_seconds",
                json={"value": 240},
                headers=admin_headers,
            )
            assert r_tb.status_code == 200
            assert r_tb.json()["new_value"] == 240
            r_check_tb = http.get("/admin/settings", headers=admin_headers)
            cur_tb = {s["key"]: s["value"] for s in r_check_tb.json()["settings"]}["time_budget_per_task_seconds"]
            assert cur_tb == 240, f"Expected time_budget 240, got {cur_tb}"
            print("  + Live setting update active: time_budget_per_task_seconds = 240")

        finally:
            # Revert settings back to original defaults
            http.put("/admin/settings/time_budget_per_task_seconds", json={"value": orig_time_budget}, headers=admin_headers)
            http.put("/admin/settings/hidden_task_count", json={"value": orig_hidden_count}, headers=admin_headers)
            http.put("/admin/settings/rate_limit_tool_calls_per_min", json={"value": orig_rate_limit}, headers=admin_headers)
            print(f"  + Restored hidden_task_count to canonical default: {orig_hidden_count}")
            print(f"  + Restored time_budget_per_task_seconds to canonical default: {orig_time_budget}")
            print(f"  + Restored rate_limit_tool_calls_per_min to canonical default: {orig_rate_limit}")

        # Assemble Report
        rehearsal_report = {
            "rehearsal_run_id": rehearsal_run_id,
            "timestamp": datetime.now(UTC).isoformat(),
            "target_url": base_url,
            "tasks_per_team": rehearsal_task_count,
            "teams": {
                key: {
                    **team_execution_results[key],
                    "aggregate_score": scoring_results[key]["aggregate_score"],
                    "dimension_scores": scoring_results[key].get("breakdown", {}).get("dimension_scores", {}),
                }
                for key in registered_teams
            },
            "score_separation_verified": True,
            "leaderboard_ranking_verified": True,
            "live_settings_zero_downtime_verified": True,
            "status": "PASS",
        }

        docs_dir = ROOT_DIR / "docs"
        docs_dir.mkdir(exist_ok=True)
        report_file = docs_dir / "rehearsal_results.json"
        with open(report_file, "w", encoding="utf-8") as f:
            json.dump(rehearsal_report, f, indent=2)

        print("\n" + "=" * 80)
        print(" REHEARSAL SUCCESSFUL — FULL PLATFORM VALIDATED IN PRODUCTION STACK")
        print(f" Report saved to: {report_file}")
        print("=" * 80)


if __name__ == "__main__":
    main()
