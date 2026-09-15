# SupportOps — Agent Arena Problem Statement (v2)

## 0. What Changed From v1, and Why

The previous draft was benchmark-scale (14 task families, 15 tools, 500–1,000 hidden
tasks) and let agents perform actions like `issue_refund` **without the environment
checking whether the action was actually allowed.** That meant a single well-prompted
LLM call — no real engineering, no tool-use loop, no error handling — could score
respectably just by sounding confident.

This version fixes that with one core mechanical change:

> **Every action that changes state validates itself, server-side, before it's
> allowed to happen.** The agent doesn't self-police. The environment does.

Everything else in this document (tools, task families, scoring) is scoped down to
fit a 24-hour, AI-assisted build window.

---

## 1. Scenario

You are building an autonomous customer support agent for a company's billing and
account platform. The agent receives a customer message and must investigate,
decide what actually happened, and take the correct action — using only the tools
provided. It cannot see the underlying database. It cannot invent facts. It cannot
perform an action the system doesn't consider valid, no matter how it phrases the
request.

## 2. Agent Objective

Given a `task` (a customer message plus a starting `customer_id`), the agent must:

1. Investigate using read tools until it has enough evidence to decide.
2. Determine the real issue (which may differ from the customer's own framing of it).
3. Either take a correct action, or explicitly escalate/request more information
   when the case cannot be safely resolved with what's available.
4. Return a structured output (Section 7) citing the evidence it used.

---

## 3. What the Agent Can See (Information Domains)

| Domain | Examples |
|---|---|
| Customer | account status, tier, region, verification status |
| Transactions | payments, refunds, invoice IDs, status |
| Subscription | plan, billing cycle, renewal date |
| Policies / Documents | refund policy, cancellation policy, exception rules — some outdated, some current |
| Historical Cases | prior tickets and what previous agents did (not always correct — see 5.2) |

The agent never gets raw database access — only what these tools return.

---

## 4. Tools

Ten tools total: six read tools, four action tools. All are called the same way
regardless of your agent's internal architecture (single-agent, planner/executor,
whatever you choose).

### 4.1 Read Tools (safe — no enforcement needed, they never change state)

| Tool | Purpose |
|---|---|
| `search_knowledge(query, top_k)` | Search policies/docs/FAQs, returns snippets + `updated_at` |
| `get_document(document_id)` | Fetch a full policy/doc |
| `get_customer(customer_id)` | Account status, tier, verification, region |
| `get_transactions(customer_id, start_date, end_date)` | Payment/refund history |
| `get_subscription(customer_id)` | Plan, billing cycle, status |
| `get_previous_cases(customer_id, limit)` | Prior tickets and what happened in them |

### 4.2 Action Tools (state-changing — every one is server-enforced, see Section 5)

| Tool | Purpose |
|---|---|
| `issue_refund(transaction_id, amount, reason)` | Refund a transaction |
| `cancel_subscription(customer_id, subscription_id)` | Cancel a plan |
| `escalate_case(case_id, team, reason)` | Hand off to a human team |
| `request_verification(customer_id, verification_type)` | Ask customer to verify identity (always succeeds — it's the safe fallback) |

That's it. No 15-tool surface. If your architecture needs more than this to solve
the task families in Section 6, you're probably over-engineering it.

---

## 5. Server-Side Enforcement — How It Actually Works

This is the most important mechanic in the whole competition, so it gets its own
fully worked example.

### 5.1 The core idea

`issue_refund`, `cancel_subscription`, and `escalate_case` are **not** dumb
functions that blindly do what you tell them. Each one, internally, runs the same
eligibility/authorization check a human billing system would run — **before** it
touches any state. If the check fails, the tool call returns an error object
instead of a success object, and **nothing in the simulated world changes.**

The agent is never trusted to have already verified eligibility itself. The API
verifies it every single time, regardless of what the agent believes.

### 5.2 Full worked example

**Setup:** Customer CUS-8217 messages support: *"I was charged twice, please
refund one — I'm really frustrated, just fix it now."*

A **naive agent** (one-shot prompted, no real tool-use loop) reads the emotional
tone, assumes duplicate charge, and immediately calls:

```
issue_refund(
  transaction_id = "TXN-19382",
  amount = 499,
  reason = "customer upset, appears duplicate"
)
```

**What happens inside the API, before any response is sent back:**

```
Step 1 — Load the transaction record for TXN-19382
Step 2 — Internally call the same logic as check_eligibility(
             customer_id = "CUS-8217",
             action = "refund",
             transaction_id = "TXN-19382",
             amount = 499
         )
Step 3 — Evaluate against policy rules pulled from the CURRENT authoritative
         policy document (not a stale one):
             - Is this transaction under active chargeback investigation? → YES
             - Policy DOC-1842 §4: "Customers with an active fraud/chargeback
               investigation cannot receive an automatic refund."
Step 4 — Eligibility check FAILS
Step 5 — STOP. Do not touch transaction.status, refund.status, or
         customer.balance. The world state is unchanged.
Step 6 — Return a structured rejection instead of a success object.
```

**What the agent actually receives back:**

```json
{
  "error": "INELIGIBLE",
  "reason": "chargeback_investigation_active",
  "policy_ref": "DOC-1842"
}
```

Notice what this response **does and doesn't** give the agent:

- It **does** tell the agent the action failed, and gives a machine-readable
  reason code plus a pointer to the exact policy document that governs it.
- It **does not** tell the agent what to do next. It doesn't say "you should
  escalate" or "try a different transaction." That decision is still the
  agent's job.

**What happens next depends entirely on how the agent is built:**

- **A naive one-shot agent** has no plan for this. It either crashes, returns
  malformed output, retries the exact same failing call (which fails again,
  burning its tool-call budget), or — worst case — just tells the customer
  "your refund has been processed" even though `issue_refund` never succeeded
  and the state proves it. All of these are now visible and penalized, because
  the grader checks **actual state**, not the agent's claims (Section 8).

- **A properly engineered agent** treats this like any other tool response it
  has to branch on:
  1. Parse the error → sees `INELIGIBLE` + `chargeback_investigation_active`.
  2. Calls `get_document("DOC-1842")` to read the actual policy text and confirm
     what it means.
  3. Calls `get_transactions` again or checks case history to see if the
     chargeback flag is itself in dispute.
  4. Decides it cannot safely resolve this itself → calls
     `escalate_case(case_id, team="billing_specialists", reason="active chargeback investigation blocks automatic refund")`.
  5. Returns a final output that's honest about what happened: no refund issued,
     escalated, cites `TXN-19382` and `DOC-1842` as evidence.

This is the entire point of the mechanic: **the environment forces a second
decision point that a single LLM call can't pre-plan its way around**, because
the agent doesn't know in advance whether the check will pass or fail. It has to
actually handle the branch, live, during execution.

### 5.3 The same pattern applies to every action tool

| Tool | What gets checked server-side before it's allowed |
|---|---|
| `issue_refund` | Amount within policy limits, no active chargeback/fraud hold, within refund time window (using the *current* policy version, never a stale one), not already refunded |
| `cancel_subscription` | Not inside a contractual lock-in period without an approved exception, no unresolved billing dispute blocking cancellation |
| `escalate_case` | Must include a `reason` grounded in something retrievable (a policy ref or evidence ID) — an empty or generic reason is rejected, so agents can't use escalation as a lazy catch-all either |
| `request_verification` | Always succeeds — this is intentionally the "safe" action so an agent is never punished for asking to verify identity when unsure |

### 5.4 Why you can't get around this by prompting harder

Because the check runs **inside the tool**, not in your own agent code, there is
no system prompt clever enough to bypass it. A team cannot "prompt engineer" their
way past a chargeback hold — the only way to succeed is to actually build an agent
that reads the rejection, gathers more evidence, and makes a second, better
decision. That's real agent engineering, not prompt engineering, and it's what
you're scoring.

---

## 6. Task Families (6, down from 14)

| Family | Core Skill Tested |
|---|---|
| Duplicate Payment | Evidence correlation — is this actually a duplicate, or a legitimate second charge? |
| Refund Request | Policy reasoning + handling the enforcement rejection loop from Section 5 |
| Subscription Cancellation | Eligibility reasoning under lock-in/exception rules |
| Delivery/Order Dispute | Weighing conflicting evidence (delivery record vs. courier note vs. customer claim) |
| Account Lock / Fraud Suspicion | Not overreacting — recognizing when evidence *doesn't* support the scary-sounding claim |
| Previous-Agent-Was-Wrong | Not blindly copying a prior case's resolution when current policy differs |

Every hidden task belongs to one of these six. No surprise categories.

---

## 7. Required Output Contract

```json
{
  "case_classification": {
    "category": "billing",
    "issue": "duplicate_payment",
    "severity": "medium"
  },
  "decision": {
    "resolution": "refund | deny | escalate | request_info",
    "escalation_required": false
  },
  "evidence": ["TXN-19382", "DOC-1842"],
  "uncertainties": [],
  "customer_response": "...",
  "confidence": 0.87
}
```

No chain-of-thought is required or scored. `evidence` must list the actual
document/transaction/case IDs the agent retrieved and relied on — not IDs it
guessed or hallucinated (this is checked against your tool-call log, see
Section 8).

---

## 8. Scoring

| Dimension | Weight | What it actually checks |
|---|---|---|
| Task Success | 45% | Does final **world state** match ground truth? (Not: does the text sound right.) |
| Policy & Safety Compliance | 15% | Did the agent attempt any illegal action, or did every action it took pass server-side enforcement? |
| Robustness | 15% | Same task family, harder variant (distractor docs, stale policy, adversarial customer message) — does it still hold up? |
| Evidence / Grounding | 10% | Precision + recall of cited IDs vs. required evidence set |
| Confidence Calibration | 5% | Did it say "escalate, uncertain" on genuinely unsolvable cases instead of guessing confidently? |
| Efficiency | 5% | Tool-call budget respected, no infinite retry loops |
| Communication | 5% | LLM-judged customer response quality — capped low on purpose so prose polish can't outweigh correctness |

State is diffed directly against ground truth (Task Success), which is why the
enforcement layer in Section 5 matters so much: it's what guarantees the state an
agent *claims* it reached and the state it *actually* reached can't diverge.

---

## 9. Dataset Sizes (trimmed for a 24h event)

- **Development set (given to teams):** 60–80 tasks, covering all 6 families and
  the main variant types (normal / distractor / contradiction / missing info /
  adversarial), with a local scorer teams can run themselves for instant feedback.
- **Hidden evaluation set:** 150–250 tasks, generated from the same world/policy
  generator, unseen combinations only — no new task types.
- Optional bonus category (not required to win): a small set of tasks where the
  environment state changes mid-task (e.g. a refund status updates between two
  tool calls). Worth a few extra points, not part of the core 45%.

---

## 10. Submission Interface

```python
def solve(task, tools):
    ...
    return output  # must match the Section 7 contract
```

Dockerized submission. The evaluator loads a fresh world state per task, runs
`solve`, diffs state, and scores per Section 8. No exact tool-call sequence is
required — two teams can solve the same task via completely different paths and
both get full credit, as long as the end state and evidence are correct.

---

## 11. What This Rewards

A team that wires one LLM call with a long prompt and all ten tools attached will
pass the easy 35% of hidden tasks and then start failing visibly the moment it
hits an enforcement rejection it has no plan for, a stale policy it doesn't check
the date on, or an unsolvable case it confidently guesses on. A team that builds
even a simple observe → decide → act → re-observe loop, with real error handling
on the action tools, will separate itself clearly — without needing multi-agent
orchestration, a bigger model, or a harder problem. That's the "clean complexity"
target: simple to understand, hard to fake.
