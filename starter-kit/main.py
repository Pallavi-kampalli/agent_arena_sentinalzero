"""Agent Arena SupportOps — Main Participant Runtime.

Orchestrates the lifecycle around the participant's agent:
1. Loads .env configuration (BASE_URL, BEARER_TOKEN, MODE).
2. Connects to the Arena API (Mock Simulator or Live Platform).
3. Executes in either:
   - Practice Mode: ad-hoc testing with --once or --max-tasks, detailed practice feedback, and poll intervals.
   - Submission Mode: full competition epoch running all tasks sequentially without artificial gaps, collecting answers in memory, submitting each task to satisfy the assignment lock, and finalizing the submission.
4. Invokes agent.solve(task, tools) strictly sequentially to prevent rate limit bottlenecks.
5. In Mock Simulator, tracks and displays accuracy (e.g. 25/30 tasks correct) and links to the visual debugger at /dashboard.
"""

import argparse
import os
import sys
import time
from typing import Any

# Load .env if python-dotenv is available
try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass

import agent
from sdk.tools_client import ApiError, ArenaClient, TransportError


def validate_output_contract(output: Any) -> list[str]:
    """Validates returned agent answer dictionary against Section 7 output contract."""
    errors = []
    if not isinstance(output, dict):
        return ["Output must be a dictionary."]

    # case_classification
    cc = output.get("case_classification")
    if not isinstance(cc, dict):
        errors.append("Missing or invalid 'case_classification' (must be dict).")
    else:
        if not isinstance(cc.get("category"), str):
            errors.append("case_classification.category must be a string.")
        if not isinstance(cc.get("issue"), str):
            errors.append("case_classification.issue must be a string.")
        if cc.get("severity") not in ("low", "medium", "high", "critical"):
            errors.append("case_classification.severity must be one of: 'low', 'medium', 'high', 'critical'.")

    # decision
    dec = output.get("decision")
    if not isinstance(dec, dict):
        errors.append("Missing or invalid 'decision' (must be dict).")
    else:
        if dec.get("resolution") not in ("refund", "deny", "escalate", "request_info"):
            errors.append("decision.resolution must be one of: 'refund', 'deny', 'escalate', 'request_info'.")
        if not isinstance(dec.get("escalation_required"), bool):
            errors.append("decision.escalation_required must be a boolean.")

    # evidence
    ev = output.get("evidence")
    if not isinstance(ev, list):
        errors.append("Missing or invalid 'evidence' (must be list of string IDs).")

    # uncertainties
    unc = output.get("uncertainties")
    if not isinstance(unc, list):
        errors.append("Missing or invalid 'uncertainties' (must be list of strings).")

    # customer_response
    resp = output.get("customer_response")
    if not isinstance(resp, str) or len(resp.strip()) == 0:
        errors.append("Missing or invalid 'customer_response' (must be non-empty string).")

    # confidence
    conf = output.get("confidence")
    if not isinstance(conf, (int, float)) or not (0.0 <= conf <= 1.0):
        errors.append("confidence must be a float between 0.0 and 1.0.")

    return errors


def main(
    mode: str = "practice",
    once: bool = False,
    max_tasks: int | None = None,
    poll_interval: float | None = None,
) -> None:
    base_url = os.getenv("BASE_URL", "http://localhost:8000")
    token = os.getenv("BEARER_TOKEN", "dev-practice-token")
    is_mock = ":8001" in base_url or "localhost:8001" in base_url or "127.0.0.1:8001" in base_url

    # Default poll intervals: 0.0 for submission (sequential throughput), 1.0 for practice
    if poll_interval is None:
        poll_interval = 0.0 if mode == "submission" else 1.0

    print("=" * 65)
    print("  Agent Arena SupportOps — Participant Runtime (main.py)")
    print("=" * 65)
    print(f"Target Arena : {base_url}")
    print(f"Auth Token   : {token[:6]}***")
    print(f"Mode         : {mode.upper()}{' (Single Task)' if once and mode == 'practice' else ''}")
    print("Sequential   : Yes (Protected against concurrency rate limits)")
    if is_mock:
        print(f"Debug UI     : {base_url.rstrip('/')}/dashboard")
    print("=" * 65)

    try:
        with ArenaClient(base_url=base_url, token=token) as client:
            sub_id = None
            # 1. Initialize or connect to active submission
            try:
                sub = client.start_submission()
                sub_id = sub.get("submission_id")
                total_expected = sub.get("tasks_total")
                print(f"[+] Started new submission: {sub_id} (Expected tasks: {total_expected or 'N/A'})")
            except ApiError as e:
                if "ACTIVE_SUBMISSION_EXISTS" in str(e):
                    if isinstance(e.detail, dict) and "submission_id" in e.detail:
                        sub_id = e.detail["submission_id"]
                    print(f"[*] Resumed existing active submission: {sub_id}")
                else:
                    print(f"[*] Submission notice: {e.detail}")

            tasks_completed = 0
            passed_count = 0
            failed_count = 0
            completed_in_memory: list[dict[str, Any]] = []
            total_start_time = time.time()

            # 2. Main task acquisition and sequential dispatch loop
            while True:
                if max_tasks and tasks_completed >= max_tasks:
                    print(f"\n[*] Reached maximum tasks limit ({max_tasks}). Exiting.")
                    break

                print(f"\n--- Requesting Task #{tasks_completed + 1} ---")
                try:
                    task = client.get_task()
                except ApiError as e:
                    err_str = str(e)
                    if "NO_MORE_TASKS" in err_str or "SUBMISSION_COMPLETED" in err_str or "NO_TASKS" in err_str:
                        print("[+] All available tasks completed for this epoch!")
                        break
                    print(f"[-] Could not acquire task: {e.detail}")
                    break

                task_id = task.get("task_id", "UNKNOWN")
                print(f"Assigned Task : {task_id}")
                print(f"Customer ID   : {task.get('customer_id')}")
                print(f"Customer Msg  : {task.get('customer_message')}")
                print("-" * 65)

                # 3. Invoke participant agent solve(task, tools) sequentially
                t0 = time.time()
                try:
                    answer = agent.solve(task, client.tools)
                except NotImplementedError:
                    print("\n" + "!" * 65)
                    print("  [!] agent.solve() raised NotImplementedError.")
                    print("  To solve tasks, implement your agent logic inside:")
                    print("      agent.py -> def solve(task, tools)")
                    print("!" * 65)
                    print("=" * 65)
                    print("Run completed successfully.")
                    return
                except Exception as e:
                    print(f"\n[-] Unhandled exception in agent.solve() on {task_id}: {e}")
                    raise

                duration = time.time() - t0

                # 4. Validate Section 7 contract compliance
                validation_errors = validate_output_contract(answer)
                if validation_errors:
                    print(f"[!] Output validation warnings for {task_id}:")
                    for err in validation_errors:
                        print(f"    - {err}")

                # 5. Submit answer to API
                res_choice = answer.get("decision", {}).get("resolution")
                esc_choice = answer.get("decision", {}).get("escalation_required")
                print(f"Resolution    : {res_choice} (Escalate: {esc_choice})")
                print(f"Evidence      : {answer.get('evidence')}")
                print(f"Solve Time    : {duration:.2f}s")
                print("Submitting resolution to API...")

                result = client.submit_task(task_id=task_id, payload=answer)
                tasks_completed += 1

                # 6. Evaluation feedback (Practice / Mock Simulator vs Hidden Live Arena)
                is_correct = None
                if "correct" in result:
                    is_correct = bool(result.get("correct", False))
                    if is_correct:
                        passed_count += 1
                        print("Mock Evaluation : [PASS] Ground truth matched perfectly!")
                    else:
                        failed_count += 1
                        print("Mock Evaluation : [FAIL]")
                        print(f"   Expected Res: {result.get('expected_resolution')}")
                        print(f"   Expected Ev:  {result.get('expected_evidence')}")
                        print(f"   Diff:         {result.get('diff_explanation')}")
                        if is_mock:
                            print(f"   Debug URL:    {base_url.rstrip('/')}/dashboard")
                else:
                    print("[+] Submission received and recorded by Arena platform.")

                completed_in_memory.append({
                    "task_id": task_id,
                    "resolution": res_choice,
                    "escalation_required": esc_choice,
                    "duration": duration,
                    "correct": is_correct,
                })

                if mode == "practice" and once:
                    print("\n[+] Single task completed (--once flag). Exiting.")
                    break

                if poll_interval > 0:
                    time.sleep(poll_interval)

            # 7. Finalize submission if in submission mode
            if sub_id and mode == "submission":
                try:
                    client.finalize_submission(sub_id)
                    print(f"\n[+] Finalized submission '{sub_id}'.")
                except ApiError as e:
                    print(f"\n[*] Finalize notice: {e.detail}")

            total_elapsed = time.time() - total_start_time
            avg_time = (total_elapsed / tasks_completed) if tasks_completed > 0 else 0.0

            # 8. Execution Summary Report
            print("\n" + "=" * 65)
            print("  EPOCH EXECUTION SUMMARY")
            print("=" * 65)
            print(f"Mode               : {mode.upper()}")
            print(f"Tasks Processed    : {tasks_completed}")
            print(f"Total Epoch Time   : {total_elapsed:.2f}s (avg {avg_time:.2f}s/task)")

            if is_mock or (passed_count + failed_count > 0):
                total_eval = passed_count + failed_count
                acc = (passed_count / total_eval * 100.0) if total_eval > 0 else 0.0
                print(f"Mock Score         : {passed_count}/{total_eval} tasks correct ({acc:.1f}%)")
                if is_mock:
                    print(f"Debug Dashboard    : {base_url.rstrip('/')}/dashboard")
            else:
                print("Arena Evaluation   : Completed and scored on hidden competition dataset.")
                print("                     (Live leaderboard remains hidden during competition)")

            print("=" * 65)

    except TransportError as e:
        print(f"\n[-] Network / Transport Error: {e}")
        print("Tip: Make sure the target Arena or Mock Simulator is running:")
        print(f"     Target URL: {base_url}")
        print("     To start mock simulator: python mock_simulator/server.py")
        sys.exit(1)
    except ApiError as e:
        print(f"\n[-] Arena API Error ({e.status_code}): {e.detail}")
        sys.exit(1)

    print("Run completed successfully.")


if __name__ == "__main__":
    default_mode = os.getenv("MODE", "practice").lower()
    if default_mode not in ("practice", "submission"):
        default_mode = "practice"

    parser = argparse.ArgumentParser(description="Agent Arena SupportOps Participant Runtime")
    parser.add_argument(
        "--mode",
        choices=["practice", "submission"],
        default=default_mode,
        help="Runtime execution mode: 'practice' (interactive/debug) or 'submission' (competition epoch)",
    )
    parser.add_argument("--once", action="store_true", help="Process only one task and exit (practice mode only)")
    parser.add_argument("--max-tasks", type=int, default=None, help="Maximum number of tasks to process")
    parser.add_argument(
        "--poll-interval",
        type=float,
        default=None,
        help="Seconds to wait between task queries (default: 1.0 for practice, 0.0 for submission)",
    )
    args = parser.parse_args()

    main(
        mode=args.mode,
        once=args.once,
        max_tasks=args.max_tasks,
        poll_interval=args.poll_interval,
    )
