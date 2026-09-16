# Agent Arena: SupportOps — Participant Starter Kit

Welcome to **Agent Arena: SupportOps**. This repository contains the official competition SDK, local development simulator, sample tabular datasets, and runtime harness for building autonomous customer support agents.

---

## 1. Architecture: Clean Separation of Concerns

The starter kit enforces a clean, modular boundary between the **runtime/orchestration layer** (`main.py`) and the **participant agent** (`agent.py`):

```text
main.py
    ↓
gets task from API (Mock Simulator or Live Arena)
    ↓
provides task + ToolsClient
    ↓
agent.solve(task, tools)
    ↓
gets returned answer
    ↓
submits answer to API
```

### Responsibility Breakdown:

| Component | File | Responsibilities |
|:---|:---|:---|
| **Participant Agent** | `agent.py` | **The ONLY file participants edit.**<br>• Reason over customer inquiry<br>• Query relevant knowledge and account tools<br>• Take eligible corrective actions<br>• Formulate decision and return final Section 7 answer |
| **Orchestration Runtime** | `main.py` | • Loads `.env` configuration (`BASE_URL`, `BEARER_TOKEN`, `MODE`)<br>• Connects to API using `ArenaClient`<br>• Runs in **Practice** or **Submission** mode<br>• Dispatches tasks sequentially to `agent.solve` (rate limit safe)<br>• Validates output contract schema<br>• Submits final answer to API and displays feedback |
| **Tools SDK** | `sdk/tools_client.py` | • `ToolsClient`: Exposes ONLY the 10 domain tools to `agent.py`<br>• `ArenaClient`: Manages task retrieval and submission for `main.py` |
| **Local Mock Simulator** | `mock_simulator/` | • Offline FastAPI/SQLite server preloaded with **30 development tasks** for local testing<br>• Built-in visual debug dashboard at `http://127.0.0.1:8001/dashboard` |
| **Sample Data Export** | `sample_data/` | • Pre-dumped CSV files representing the mock world state (Seed 1000) and tasks with full answers<br>• For human exploration and understanding only |

---

## 2. Participant Entry Point: `agent.py`

`agent.py` is the only file you modify. Its interface is minimal and clean:

```python
from typing import Any
from sdk.tools_client import ToolsClient


def solve(task: dict[str, Any], tools: ToolsClient) -> dict[str, Any]:
    """Implement your autonomous customer support agent here."""
    raise NotImplementedError
```

`agent.py` contains **ZERO** orchestration code:
- No API connection logic
- No authentication handling
- No task fetching or polling loops
- No submission handling
- No environment loading

The participant receives only `task` and `tools`, and returns the final answer.

---

## 3. Task Input

When `main.py` calls `agent.solve(task, tools)`, `task` contains:

```python
{
    "task_id": "TASK-DEV-0001",
    "customer_id": "CUS-1001050",
    "customer_message": "Hello, I was charged twice on my card for $99.0. Please refund the extra charge immediately."
}
```

### Field Definitions:
- **`task_id`** (`str`): Unique identifier for the assigned task instance. Used when escalating cases (`tools.escalate_case(case_id=task_id, ...)`).
- **`customer_id`** (`str`): Customer identifier. Used with read and action tools to query profile, transactions, subscriptions, and history.
- **`customer_message`** (`str`): The inbound inquiry or dispute submitted by the customer.

---

## 4. Tools Available on `tools: ToolsClient`

The `tools` argument provides access to **ALL 10 participant-facing tools**:

### Read Tools (Safe — Inspect state without mutations)

#### 1. `tools.search_knowledge(query: str, top_k: int = 5) -> dict[str, Any]`
- **Parameters**: `query` (search keywords), `top_k` (maximum results, default 5).
- **Returns**: `{"results": [{"id": "DOC-1002", "title": "...", "snippet": "...", "category": "...", "updated_at": "..."}]}`
- **Purpose**: Searches policy manuals, FAQs, and terms of service.

#### 2. `tools.get_document(document_id: str) -> dict[str, Any]`
- **Parameters**: `document_id` (e.g. `"DOC-1002"`).
- **Returns**: `{"document": {"id": "DOC-1002", "title": "...", "content": "...", "category": "...", "updated_at": "..."}}`
- **Purpose**: Retrieves the full text and clauses of a specific policy document.

#### 3. `tools.get_customer(customer_id: str) -> dict[str, Any]`
- **Parameters**: `customer_id` (e.g. `"CUS-1001050"`).
- **Returns**: `{"customer": {"id": "...", "name": "...", "email": "...", "tier": "...", "verification_status": "...", "account_status": "..."}}`
- **Purpose**: Retrieves customer profile, tier, and account status.

#### 4. `tools.get_transactions(customer_id: str, start_date: str | None = None, end_date: str | None = None) -> dict[str, Any]`
- **Parameters**: `customer_id`, optional ISO timestamp bounds `start_date`, `end_date`.
- **Returns**: `{"transactions": [{"id": "TXN-8028", "amount": 99.0, "status": "settled", ...}]}`
- **Purpose**: Lists transactions associated with the customer account.

#### 5. `tools.get_subscription(customer_id: str) -> dict[str, Any]`
- **Parameters**: `customer_id`.
- **Returns**: `{"subscription": {"id": "SUB-101", "plan": "pro_monthly", "status": "active", ...}}`
- **Purpose**: Retrieves current active subscription details for the customer.

#### 6. `tools.get_previous_cases(customer_id: str, limit: int = 5) -> dict[str, Any]`
- **Parameters**: `customer_id`, `limit` (default 5).
- **Returns**: `{"cases": [{"case_id": "CASE-901", "resolution": "refund", "was_correct": false, ...}]}`
- **Purpose**: Retrieves past support tickets and agent actions (essential for `previous_agent_was_wrong` disputes).

---

### Action Tools (State Mutating — Server-enforced eligibility)

#### 7. `tools.issue_refund(transaction_id: str, amount: float, reason: str) -> dict[str, Any]`
- **Requires Prior Read**: You must read the relevant transaction or policy before executing this action.
- **Enforcement**: Checks transaction age, chargeback status, dispute locks, and amount limits.

#### 8. `tools.cancel_subscription(subscription_id: str, immediate: bool, reason: str) -> dict[str, Any]`
- **Requires Prior Read**: You must read the customer subscription or cancellation policy first.
- **Enforcement**: Verifies minimum commitment period and open billing dispute status.

#### 9. `tools.escalate_case(case_id: str, department: str, reason: str, priority: str = "medium") -> dict[str, Any]`
- **Requires Prior Read**: Requires grounded evidence from customer history, transactions, or policy rules.
- **Enforcement**: Validates department choices (`tier_3_technical`, `fraud_prevention`, `legal_and_compliance`, etc.) and priority justification.

#### 10. `tools.request_verification(customer_id: str, verification_type: str, reason: str) -> dict[str, Any]`
- **Requires Prior Read**: Requires customer profile retrieval.
- **Enforcement**: Used for identity verification and anti-fraud lock requirements.

---

## 5. Output Contract (Section 7)

Your `agent.solve(task, tools)` function must return a dictionary conforming to the standard Section 7 contract:

```python
{
    "case_classification": {
        "category": "billing",       # Category string
        "issue": "duplicate_charge", # Specific issue identifier
        "severity": "medium",        # One of: "low", "medium", "high", "critical"
    },
    "decision": {
        "resolution": "refund",      # One of: "refund", "deny", "escalate", "request_info"
        "escalation_required": False # True if passed to human/specialist
    },
    "evidence": [
        "TXN-DUP-A-8028", "TXN-DUP-B-2624", "DOC-1002"
    ],
    "uncertainties": [],
    "customer_response": "We have identified the duplicate charge and issued a full refund of $99.00.",
    "confidence": 0.95
}
```

---

## 6. Sample Mock Data (`sample_data/`)

The `sample_data/` folder contains exported CSV files from the mock environment (Seed 1000):
- `tasks.csv`: The 30 development tasks **with full ground truth reference answers**.
- `customers.csv`: Customer profile records.
- `transactions.csv`: Transaction history and refund statuses.
- `subscriptions.csv`: Recurring subscription plans and cancellation lock periods.
- `policies.csv`: Authoritative policies and operating procedures.
- `previous_cases.csv`: Historical support tickets.

> **CRITICAL ARCHITECTURAL NOTE:**
> - The mock dataset (Seed 1000) and the live competition dataset (Seed 50000+) are **completely disjoint**.
> - Live evaluation features unseen customer names, transaction IDs, subscription dates, and edge cases.
> - **DO NOT hardcode answers or rules based on these CSV files.** Your agent must fetch evidence and verify policies dynamically via the `tools` client.

---

## 7. Execution Modes & Running the Agent

### Environment Setup
```bash
# Create virtualenv and install dependencies
python -m venv .venv

# Activate:
.\.venv\Scripts\activate      # Windows
source .venv/bin/activate       # Linux / macOS

pip install -r requirements.txt
pip install -r mock_simulator/requirements.txt
```

### Execution Modes

#### Mode A: Practice Mode (`--mode practice`)
Designed for interactive testing and local debugging:
- Runs ad-hoc tasks with optional poll intervals.
- In mock simulator, returns immediate ground-truth diff feedback for each task.
- CLI examples:
  ```bash
  python main.py --mode practice --once             # Process 1 task and stop
  python main.py --mode practice --max-tasks 5      # Process 5 tasks
  ```

#### Mode B: Submission Mode (`--mode submission`)
Designed for full competition epoch execution:
- Initializes a submission run with the Live Arena API (`POST /submission/start`).
- Receives all **30 benchmark tasks** upfront in randomized order with ephemeral IDs.
- Solves each task sequentially in memory with active `X-Task-ID` tool calls (rate-limit safe).
- Submits all 30 task solutions in a single atomic batch (`POST /submission/{id}/submit`).
- Auto-timeout protection: submissions taking >30 minutes or interrupted by critical errors are marked `interrupted` and not penalized.
- CLI example:
  ```bash
  python main.py --mode submission
  ```

---

## 8. Mock Simulator Debugger (`/dashboard`)

When testing against the local mock simulator, open:
**`http://127.0.0.1:8001/dashboard`**

The dashboard provides complete local ground truth verification:
- **Score Report**: Real-time pass/fail count and accuracy percentage (`X / 30 tasks correct`).
- **Failed Tasks**: Full diff analysis comparing your agent's resolution, escalation, and evidence against the ground truth.
- **Passed Tasks**: Expandable detail cards showing the exact customer message, ground truth matches, and tool call traces.
- **Auto-Refresh & Reset**: Live auto-refresh as your agent solves tasks, with a one-click reset button.
- **Privacy Guaranteed**: The live competition leaderboard and hidden test evaluations are never exposed to participants.
