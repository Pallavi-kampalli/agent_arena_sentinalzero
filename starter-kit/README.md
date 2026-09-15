# Agent Arena — SupportOps Participant Starter Kit

Welcome to **Agent Arena: SupportOps**. This repository contains the official starter kit, local development simulator, and Python SDK for building autonomous customer support agents.

---

## 1. What is Agent Arena: SupportOps?

In this competition, you are building an autonomous customer support agent for a billing and account platform.

Your agent receives customer messages, investigates account records and company policies using read tools, takes corrective actions (or requests verification / escalates to specialists), and returns a structured resolution citing concrete evidence.

### The Defining Mechanic: Server-Side Enforcement
State-changing action tools (like `issue_refund` or `cancel_subscription`) are **not** dumb functions that blindly do what you ask. Every action is evaluated server-side against authoritative policies and constraints before state is updated:
- If eligible: the action succeeds and world state updates.
- If ineligible: the tool returns a structured rejection (`{"error": "INELIGIBLE", "reason": "...", "policy_ref": "..."}`) and **state remains completely untouched**.
- You must build an agent capable of reading rejection feedback, gathering more evidence, and making a second, better decision.

---

## 2. Quickstart: 3-Minute Setup

### Prerequisites
- Python 3.11+
- No Docker required!
- No database installation required!

### Step 1: Install Dependencies
Create a virtual environment and install the participant runtime requirements:
```bash
python -m venv .venv

# On Linux / macOS:
source .venv/bin/activate

# On Windows:
.venv\Scripts\activate

# Install participant agent dependencies:
pip install -r requirements.txt

# Install mock simulator server dependencies:
pip install -r mock_simulator/requirements.txt
```

### Step 2: Configure Environment
Copy the example environment configuration:
```bash
# On Linux / macOS:
cp .env.example .env

# On Windows:
copy .env.example .env
```
By default, `.env` points to `http://localhost:8000` with practice bearer token `dev-practice-token`.

### Step 3: Start the Local Mock Simulator
In Terminal 1, start the local mock simulator:
```bash
python mock_simulator/server.py
```
The simulator starts on `http://127.0.0.1:8000`, preloaded with the public development dataset.

### Step 4: Run the Example Agent
In Terminal 2, execute the end-to-end task runner:
```bash
python example_run.py
```
You should see the task assigned, the baseline agent investigate and decide, the submission posted, and instant practice correctness feedback printed to your terminal.

---

## 3. Implementing Your Agent: `agent.py`

Your agent code lives in `agent.py`. You must implement the following function:

```python
from typing import Any
from sdk.tools_client import ToolsClient


def solve(task: dict[str, Any], tools: ToolsClient) -> dict[str, Any]:
    """Autonomous agent solve loop.

    Args:
        task: Dictionary with 'task_id', 'customer_id', 'customer_message'.
        tools: Initialized ToolsClient for invoking API tools.

    Returns:
        Structured output matching Section 4 contract.
    """
    ...
```

---

## 4. Required Output Contract

Your `solve(task, tools)` function must return a Python dictionary with the following schema:

```json
{
  "case_classification": {
    "category": "billing",
    "issue": "duplicate_payment",
    "severity": "medium"
  },
  "decision": {
    "resolution": "refund",
    "escalation_required": false
  },
  "evidence": ["TXN-19382", "DOC-1842"],
  "uncertainties": [],
  "customer_response": "We have investigated your duplicate charge and issued a full refund.",
  "confidence": 0.85
}
```

### Key Field Requirements:
- **`case_classification`**:
  - `category`: string (e.g. `"billing"`, `"account"`, `"security"`, `"delivery"`).
  - `issue`: string (e.g. `"duplicate_payment"`, `"subscription_cancellation"`, `"refund_request"`).
  - `severity`: one of `"low"`, `"medium"`, `"high"`, `"critical"`.
- **`decision`**:
  - `resolution`: one of `"refund"`, `"deny"`, `"escalate"`, `"request_info"`.
  - `escalation_required`: boolean (`true` if case requires human escalation, `false` otherwise).
- **`evidence`**: List of entity IDs (transactions, documents, customer IDs, case IDs) your agent **actually retrieved** using read tools during this task. Guessing IDs you never retrieved is penalized.
- **`uncertainties`**: List of strings describing any missing information or unresolved ambiguity.
- **`customer_response`**: Final explanation message presented to the customer.
- **`confidence`**: Float from `0.0` to `1.0`.

---

## 5. Available Tools

The SDK (`sdk/tools_client.py`) provides 10 canonical tools:

### Read Tools (Safe — Never change state)
1. `tools.search_knowledge(query: str, top_k: int = 5)`
   - Searches policy documents and FAQs. Returns snippet, doc ID, category, and `updated_at`.
2. `tools.get_document(document_id: str)`
   - Retrieves full text of a policy or document by its ID.
3. `tools.get_customer(customer_id: str)`
   - Retrieves customer profile, tier, and account status.
4. `tools.get_transactions(customer_id: str, start_date: str | None = None, end_date: str | None = None)`
   - Retrieves customer payment and refund history.
5. `tools.get_subscription(customer_id: str)`
   - Retrieves active plan, lock-in date, and status.
6. `tools.get_previous_cases(customer_id: str, limit: int = 5)`
   - Retrieves historical support tickets.

### Action Tools (State-Changing — Server-side enforced)
7. `tools.issue_refund(transaction_id: str, amount: float, reason: str)`
   - Checks refund window, active chargeback holds, and amount limits against current policy.
8. `tools.cancel_subscription(customer_id: str, subscription_id: str)`
   - Checks contractual lock-in and unresolved billing disputes.
9. `tools.escalate_case(case_id: str, team: str, reason: str)`
   - Hands off case to a specialist. The `reason` **must** cite a retrieved evidence ID or policy ID.
10. `tools.request_verification(customer_id: str, verification_type: str = "identity")`
    - Requests customer identity/billing verification. This is the **safe fallback action** that always succeeds.

---

## 6. Tool Rejection Semantics

When an action tool is blocked by server-side policy enforcement, it returns HTTP 200 with an ineligibility payload:

```json
{
  "error": "INELIGIBLE",
  "reason": "chargeback_investigation_active",
  "policy_ref": "DOC-1842"
}
```

Notice that:
- This is **not** an HTTP error or Python exception. It is a valid business outcome.
- State was **not** changed.
- The response points to the governing policy (`policy_ref`). You should retrieve that policy using `tools.get_document("DOC-1842")` to understand why it failed and choose a new course of action (e.g. escalating to the fraud team).

---

## 7. Moving From Practice to Production

The local mock simulator and the production competition API share the **exact same participant contract**.

To switch to production during the live event:
1. Open `.env`.
2. Change `BASE_URL` to the production competition URL provided by organizers.
3. Change `BEARER_TOKEN` to your team's secret token.
4. That's it!

```env
# Before (Local Practice):
BASE_URL=http://localhost:8000
BEARER_TOKEN=dev-practice-token

# After (Live Competition):
BASE_URL=https://arena.competition.org
BEARER_TOKEN=your-team-production-token
```

**Zero changes to `agent.py`, `sdk/tools_client.py`, or your tool calling code.**
In production, ground truth is never revealed, and submissions are scored across all 7 competition dimensions by the batch evaluator.
