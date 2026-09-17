import json
import os
import sys
import time
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT_DIR / "starter-kit"))

import agent
from sdk.tools_client import ApiError, ArenaClient

client = ArenaClient(base_url="http://127.0.0.1:8001", token="dev-practice-token")

# Clean up any leftover active submission
try:
    sub = client.start_submission()
    sid = sub.get("submission_id")
    if sid:
        client.abort_submission(sid)
except ApiError as e:
    detail_obj = e.detail.get("detail", e.detail) if isinstance(e.detail, dict) else {}
    if isinstance(detail_obj, dict) and "submission_id" in detail_obj:
        client.abort_submission(detail_obj["submission_id"])

# Start fresh submission
sub = client.start_submission()
sub_id = sub.get("submission_id")
tasks = sub.get("tasks", [])

print("=" * 70)
print("  SENTINELZERO — MOCK SIMULATOR EVALUATION RUN")
print("=" * 70)
print(f"Submission ID : {sub_id}")
print(f"Tasks Total   : {len(tasks)}")
print("-" * 70)

answers = []

for idx, task in enumerate(tasks, 1):
    tid = task.get("task_id")
    client.set_active_task(tid)

    t0 = time.time()
    ans = agent.solve(task, client.tools)
    duration = time.time() - t0

    res = ans.get("decision", {}).get("resolution", "UNKNOWN")
    esc = ans.get("decision", {}).get("escalation_required", False)
    evidence = ans.get("evidence", [])

    answers.append(
        {
            "task_id": tid,
            "decision": ans.get("decision", {}),
            "evidence": evidence,
            "notes": ans.get("notes", ""),
            "customer_response": ans.get("customer_response", ""),
            "confidence": ans.get("confidence", 1.0),
            "case_classification": ans.get("case_classification"),
            "uncertainties": ans.get("uncertainties", []),
        }
    )

    print(
        f"Task #{idx:02d} [{tid}]: Decision={res.upper():<10} | Escalate={str(esc):<5} | Evidence={evidence} | Time={duration:.3f}s"
    )

print("-" * 70)
print("Submitting task solutions batch to Mock Simulator...")
submit_res = client.submit_batch(sub_id, answers)
print("=" * 70)
print("MOCK SIMULATOR FINAL EVALUATION REPORT:")
print(f"Status           : {submit_res.get('status')}")
print(f"Score            : {submit_res.get('score_pct')}% ({submit_res.get('tasks_submitted')}/{submit_res.get('tasks_total')})")
print(f"Passed Benchmark : {submit_res.get('passed')}")
print("=" * 70)
