import json
import os
import sys

# Attempt to load .env if python-dotenv is available
try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass

from agent import solve
from sdk.tools_client import ApiError, ToolsClient, TransportError


def main() -> None:
    base_url = os.getenv("BASE_URL", "http://localhost:8000")
    token = os.getenv("BEARER_TOKEN", "dev-practice-token")

    print("=" * 60)
    print(" Agent Arena SupportOps — Example Task Run")
    print("=" * 60)
    print(f"Target API:    {base_url}")
    print(f"Auth Token:    {token[:6]}***")
    print("-" * 60)

    try:
        with ToolsClient(base_url=base_url, token=token) as tools:
            # 0. Ensure active submission exists (works in both mock simulator and production)
            try:
                tools.start_submission()
            except ApiError as e:
                if "ACTIVE_SUBMISSION_EXISTS" not in str(e):
                    pass

            # 1. Start / assign task
            print("1. Requesting task assignment via POST /task/start...")
            task = tools.start_task()
            task_id = task["task_id"]
            print(f"   Assigned Task ID: {task_id}")
            print(f"   Customer ID:      {task['customer_id']}")
            print(f"   Customer Message: {task['customer_message']}")
            print("-" * 60)

            # 2. Run agent solve loop
            print("2. Invoking agent.solve(task, tools)...")
            output = solve(task, tools)
            print(f"   Resolution:       {output['decision']['resolution']}")
            print(f"   Must Escalate:    {output['decision']['escalation_required']}")
            print(f"   Evidence Cited:   {output['evidence']}")
            print(f"   Confidence:       {output['confidence']}")
            print(f"   Response:         {output['customer_response'][:100]}...")
            print("-" * 60)

            # 3. Submit decision
            print("3. Submitting task resolution via POST /task/submit...")
            result = tools.submit_task(
                task_id=task_id,
                case_classification=output["case_classification"],
                decision=output["decision"],
                evidence=output["evidence"],
                uncertainties=output["uncertainties"],
                customer_response=output["customer_response"],
                confidence=output["confidence"],
            )
            print("   Submission Status: RECEIVED")
            print(json.dumps(result, indent=2))

            # 4. Interpret practice feedback if mock simulator revealed ground truth
            if "correct" in result:
                print("-" * 60)
                print("PRACTICE MODE EVALUATION FEEDBACK:")
                status_str = "CORRECT [PASS]" if result.get("correct") else "INCORRECT [FAIL]"
                print(f"   Evaluation:       {status_str}")
                print(f"   Expected Res:     {result.get('expected_resolution')}")
                print(f"   Expected Ev:      {result.get('expected_evidence')}")
                print(f"   Diff Explanation: {result.get('diff_explanation')}")

            print("=" * 60)
            print("Run completed successfully.")

    except ApiError as e:
        print(f"API Error ({e.status_code}): {e.detail}")
        sys.exit(1)
    except TransportError as e:
        print(f"Transport/Network Error: {e}")
        print("Tip: Make sure the mock simulator or API is running on BASE_URL.")
        sys.exit(1)


if __name__ == "__main__":
    main()
