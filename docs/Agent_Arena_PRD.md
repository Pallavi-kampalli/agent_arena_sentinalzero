# Agent Arena — SupportOps Platform: Product Requirements Document (PRD)

**Purpose of this document:** hand this to a coding agent (with SupportOps_PS_v2.md
alongside it as the problem-statement source of truth) to build the entire
competition platform end to end — dataset/world generator, production API,
admin dashboard, mock/practice simulator, and participant starter kit.

**Scale target:** 50–100 teams. **Event length:** 24 hours. **Build time:** ~1 week,
AI-assisted.

**Note on LLM model restriction:** for this version, assume **all models are
allowed** — no whitelist enforcement is active. The architecture below still
includes a config-driven allow-list mechanism (§7) so restriction can be
switched on later purely via settings, with zero code changes. Do not remove
that mechanism; just leave it permissive by default (`allowed_llm_models: "*"`).

---

## 1. System Overview

```
                         ┌─────────────────────┐
                         │   Admin Dashboard     │  (organizer only)
                         │  register teams,      │
                         │  manage tokens,        │
                         │  live leaderboard,      │
                         │  runtime config          │
                         └──────────┬────────────┘
                                    │
                         ┌──────────▼────────────┐
                         │   Production API        │  (FastAPI, Docker Compose,
                         │  - bearer auth           │   deployable to Render or
                         │  - tool endpoints         │   self-hosted unchanged)
                         │  - task flow / submission  │
                         │  - scoring engine           │
                         └──────────┬────────────┘
                                    │
                         ┌──────────▼────────────┐
                         │      PostgreSQL          │
                         │  teams, tokens, tasks,   │
                         │  submissions, scores,     │
                         │  tool_call_logs, settings  │
                         └─────────────────────────┘

   Participant side (their own machine — no Docker required):
   ┌───────────────────────┐        ┌──────────────────────┐
   │  Starter Kit (GitHub)  │───────▶│   Mock Simulator       │  (practice only,
   │  agent.py + SDK client  │        │   local, same schema,  │   local, reveals
   │  .env (BASE_URL swap)    │        │   reveals correctness  │   ground truth)
   └────────────┬────────────┘        └──────────────────────┘
                │
                │  swap BASE_URL + bearer token, nothing else changes
                ▼
       Production API (live, hidden tasks, ground truth never revealed)
```

**Core architectural decision, confirmed with the organizer:** there is no
sandboxed execution of participant code. Agents run wherever the team wants
(their own laptop or cloud). The organizer's system is purely a **stateful API
that serves tasks and scores submitted answers** — a pull/submit/score loop,
not a code-execution platform. This removes an entire category of
infrastructure (container sandboxing, resource isolation of untrusted code)
from scope. GitHub repo links are still collected at registration for
record-keeping and the optional final defense, but are never cloned or run.

Two systems share one contract: the **Mock Simulator** (participant's laptop,
practice) and the **Production API** (organizer's, scored). Both implement
identical tool schemas from SupportOps_PS_v2.md §4, built from one shared
route-definition module with a `REVEAL_GROUND_TRUTH` flag — never maintain two
separate implementations of the same schema.

---

## 2. Core Design Principle: Everything Tunable Is Config, Not Code

Infra-level secrets live in `.env` (loaded once at process boot, changing them
requires a redeploy). Every competition-tunable parameter lives in a
`settings` table in PostgreSQL, editable live from the Admin Dashboard
**without a redeploy** — this matters because Render redeploys are slow and
requirements will change during build week and possibly mid-event.

### 2.1 `.env` (infra-level only — redeploy required to change these)
```
DATABASE_URL=
ADMIN_PANEL_SECRET=
JWT_SIGNING_SECRET=
PORT=
ENVIRONMENT=production|staging
```

### 2.2 `settings` table (competition-level — editable live, no redeploy)

| Key | Type | Default | Meaning |
|---|---|---|---|
| `submission_limit_per_team` | int | 5 | Max final submission runs allowed per team |
| `score_aggregation` | enum | `best` | `best` \| `last` \| `average` — how a team's N submissions collapse into one leaderboard score |
| `scoring_weights` | json | `{task_success:0.45, policy:0.15, robustness:0.15, evidence:0.10, calibration:0.05, efficiency:0.05, communication:0.05}` | Must sum to 1.0 — validated on save, rejected otherwise |
| `hidden_task_count` | int | 200 | Tasks served per team during a live submission run |
| `dev_task_count` | int | 70 | Tasks shipped in the public/mock dataset |
| `tool_call_budget_per_task` | int | 40 | Hard cap; further calls on that task return `429 BUDGET_EXCEEDED` |
| `time_budget_per_task_seconds` | int | 180 | Wall-clock limit per task before it's auto-marked `timed_out` |
| `competition_phase` | enum | `registration` | `registration` → `build` → `frozen` → `evaluating` → `results_published` |
| `competition_start_at` / `competition_end_at` | datetime | — | Drives automatic phase transitions |
| `allowed_llm_models` | string or json list | `"*"` (unrestricted) | Reserved for future use — see note in header. `"*"` means no restriction is enforced anywhere |
| `rate_limit_tool_calls_per_min` | int | 60 | Per-team throttle on tool endpoints |
| `bearer_token_expiry_hours` | int or null | `null` (no expiry) | Optional token lifetime |

**Build the settings service (read, write, validate, audit-log) before any
other feature reads a config value.** Nothing downstream may hardcode a
number that appears in this table.

---

## 3. Database Schema (PostgreSQL)

```sql
teams (
  team_id            UUID PRIMARY KEY,
  team_name          TEXT NOT NULL,
  members            JSONB,                 -- names/emails, freeform
  github_repo_url    TEXT,                  -- collected, never executed
  bearer_token_hash  TEXT NOT NULL,         -- store a hash only, never plaintext
  token_version      INT DEFAULT 1,         -- bump on regenerate → old token instantly invalid
  status             TEXT DEFAULT 'active', -- active | disqualified | suspended
  created_at         TIMESTAMPTZ,
  updated_at         TIMESTAMPTZ
)

tasks (                                     -- generated world state + ground truth
  task_id            TEXT PRIMARY KEY,
  dataset            TEXT,                  -- 'dev' | 'hidden'
  family             TEXT,                  -- one of the 6 SupportOps task families
  variant            TEXT,                  -- normal|distractor|contradiction|missing_info|adversarial|stale
  input_payload      JSONB,                 -- customer_message, customer_id
  world_state_seed   JSONB,                 -- everything needed to instantiate a fresh copy of the world
  ground_truth       JSONB NOT NULL,        -- NEVER exposed via any team-facing endpoint in production mode
  created_at         TIMESTAMPTZ
)

task_assignments (                          -- the task a team currently has open, and its live world copy
  id                          BIGSERIAL PRIMARY KEY,
  team_id                     UUID REFERENCES teams,
  task_id                     TEXT REFERENCES tasks,
  submission_id               UUID REFERENCES submissions,
  assigned_at                 TIMESTAMPTZ,
  world_runtime_state         JSONB         -- mutable working copy; tool calls mutate THIS, never world_state_seed
)

submissions (                               -- one row per team per final attempt
  submission_id       UUID PRIMARY KEY,
  team_id              UUID REFERENCES teams,
  attempt_number        INT,
  started_at             TIMESTAMPTZ,
  completed_at             TIMESTAMPTZ,
  status                    TEXT,           -- in_progress | completed | expired
  per_task_results          JSONB,          -- array of {task_id, scores by dimension}
  aggregate_score            NUMERIC,
  breakdown                    JSONB        -- per-dimension totals, feeds leaderboard + tiebreaks
)

tool_call_logs (
  id                     BIGSERIAL PRIMARY KEY,
  team_id                UUID,
  task_id                TEXT,
  submission_id           UUID,
  tool_name                TEXT,
  request_payload           JSONB,          -- never includes the bearer token
  response_payload            JSONB,
  was_enforcement_rejection    BOOLEAN,     -- true if an action tool returned INELIGIBLE / rejected
  latency_ms                     INT,
  created_at                       TIMESTAMPTZ
)

settings (
  key            TEXT PRIMARY KEY,
  value           JSONB,
  updated_by       TEXT,
  updated_at        TIMESTAMPTZ
)

settings_audit_log (
  id             BIGSERIAL PRIMARY KEY,
  key             TEXT,
  old_value        JSONB,
  new_value         JSONB,
  changed_by         TEXT,
  changed_at           TIMESTAMPTZ
)
```

---

## 4. Production API — Full Endpoint Spec

Every endpoint except `/health` requires `Authorization: Bearer <token>`. The
token resolves server-side to a `team_id`; teams never send their own
`team_id` in a request body — it is always derived from the token, so one
team can never query or mutate another team's data by guessing an ID.

### 4.1 Read Tools (identical shape on mock and production)

```
POST /tools/search_knowledge     { query, top_k } → { results: [...] }
POST /tools/get_document          { document_id } → { document }
POST /tools/get_customer           { customer_id } → { customer }
POST /tools/get_transactions        { customer_id, start_date, end_date } → { transactions }
POST /tools/get_subscription         { customer_id } → { subscription }
POST /tools/get_previous_cases        { customer_id, limit } → { cases }
```
Every call is logged to `tool_call_logs`. Every call resolves against the
calling team's *currently assigned* `task_assignments.world_runtime_state` —
never global state, so no team can see another team's data even with the
same `customer_id` string (customer IDs are scoped per task_assignment, not
globally unique across the platform).

### 4.2 Action Tools (server-enforced — see SupportOps_PS_v2.md §5 for the full mechanic)

```
POST /tools/issue_refund
  { transaction_id, amount, reason }
  → 200 { status: "refunded", transaction: {...} }
  → 200 { error: "INELIGIBLE", reason: "<reason_code>", policy_ref: "<doc_id>" }
    (always HTTP 200 — an ineligibility result is a valid business outcome,
     not a client/request error, and must not be treated as one)

POST /tools/cancel_subscription
  { customer_id, subscription_id }
  → same success / ineligible response pattern as above

POST /tools/escalate_case
  { case_id, team, reason }
  → 200 { error: "INVALID_ESCALATION", reason: "reason_not_grounded" }
    if `reason` does not reference an evidence ID that this team actually
    retrieved earlier in this same task (checked against tool_call_logs)
  → 200 { status: "escalated" }

POST /tools/request_verification
  { customer_id, verification_type }
  → 200 { status: "verification_requested" }   -- always succeeds; the
                                                    intentional safe fallback
```

**Non-negotiable implementation rule:** enforcement logic runs *inside* each
action tool's handler, using the exact same eligibility function the world
generator's reference solver uses to produce `ground_truth`. There must be
exactly one implementation of "what is eligible," shared by generation,
enforcement, and grading — never three separate copies that could drift out
of sync with each other.

### 4.3 Task Flow Endpoints

```
POST /task/start
  (no body — server assigns the next unassigned task for this team's
   current submission)
  → { task_id, customer_message, customer_id }
  Creates a task_assignments row, copying tasks.world_state_seed into a
  fresh world_runtime_state for this attempt.

POST /task/submit
  { task_id, case_classification, decision, evidence, uncertainties,
    customer_response, confidence }
  → { received: true, task_id }
  Grading never happens synchronously in front of the team in production
  mode — see §5. Response never includes correctness.
```

### 4.4 Submission Lifecycle Endpoints

```
POST /submission/start           → { submission_id, attempt_number, tasks_total }
GET  /submission/{id}/status      → { status, tasks_completed, tasks_total, time_remaining_seconds }
POST /submission/{id}/finalize     → { submission_id, status: "completed" }
```
A submission = one full pass through `hidden_task_count` tasks via repeated
`/task/start` + `/task/submit` calls. `submission_limit_per_team` (config)
caps how many separate submission runs a team may start in total across the
event.

### 4.5 Mock Simulator — Same Endpoints, One Additional Field

Identical routes to 4.1–4.4, with one behavioral difference on `/task/submit`:
```
POST /task/submit
  → { received: true, correct: true/false,
      expected_resolution: "...", expected_evidence: [...],
      your_evidence: [...], diff_explanation: "..." }
```
Implement this as the *same* codebase with a `REVEAL_GROUND_TRUTH` flag —
`true` in the mock's local config, `false` in production's `.env`. There is
exactly one route implementation; the flag only changes what the response
includes.

---

## 5. Scoring Engine

Runs as a batch job triggered by `/submission/{id}/finalize` — never live,
per-task, in front of the team, since that would turn the production API into
a real-time oracle mid-run.

For each completed task in a submission, compute:

1. **Task Success** — diff the final `task_assignments.world_runtime_state`
   against `tasks.ground_truth.expected_end_state`. This is a state
   comparison, not a text comparison — it is what makes the enforcement
   layer in §4.2 meaningful, since a team's claimed outcome and the actual
   outcome cannot diverge.
2. **Policy & Safety** — count of `tool_call_logs.was_enforcement_rejection`
   for this task, plus a check that the team's submitted
   `decision.resolution` matches what actually succeeded in state (not
   merely what they claimed happened).
3. **Robustness** — identical grading logic to Task Success, but rolled up
   separately for tasks where `tasks.variant != 'normal'`, so the
   leaderboard can show it as its own dimension without a second grading
   pass.
4. **Evidence** — precision and recall of submitted `evidence[]` against
   `ground_truth.required_evidence`, with the added rule that any cited ID
   must also appear in this team's own `tool_call_logs` for that task —
   an ID they never actually retrieved cannot count, even if it happens to
   be correct, which prevents reward for guessing IDs.
5. **Calibration** — if `ground_truth.must_escalate == true`, check whether
   `decision.escalation_required == true`; penalize high `confidence`
   values specifically on tasks the team got wrong.
6. **Efficiency** — tool calls used vs. `tool_call_budget_per_task`, with an
   additional penalty for repeated identical calls (a loop, not
   investigation).
7. **Communication** — a single LLM-judge call scoring `customer_response`
   against a fixed rubric (accuracy, clarity, no unsupported claims); its
   contribution to the total is capped by `scoring_weights.communication`
   (default 5%), by design, so prose quality can never outweigh
   correctness.

`submissions.aggregate_score` = weighted sum across all tasks in that run,
using `settings.scoring_weights`. A team's leaderboard score is computed from
all of that team's `submissions.aggregate_score` values per
`settings.score_aggregation` — **store every submission's score permanently
and never overwrite it**, so changing `score_aggregation` after the fact
(e.g. from `best` to `average`) is a pure recomputation, not a data loss
event.

---

## 6. Admin Dashboard — Full Feature Spec

Authenticated separately from team bearer tokens (simple admin
username/password, backed by `ADMIN_PANEL_SECRET`).

### 6.1 Team Management
- Register a team: name, members, optional GitHub repo URL → generates
  `team_id` + bearer token, shown once, downloadable as a ready-to-paste
  `.env` snippet matching the starter kit's expected variable names.
- Regenerate a team's bearer token (bumps `token_version`; the old token
  is rejected on the very next request).
- Edit team details; suspend or disqualify a team (a status flag, never a
  hard delete — all historical data is preserved for dispute resolution).
- Bulk CSV import for registering many teams at once (important at
  50–100 teams).
- Search and filter the team list.

### 6.2 Live Leaderboard
- Columns: rank, team name, aggregate score, and the per-dimension
  percentages (task success, policy, robustness, evidence, calibration,
  efficiency, communication), plus last-submission timestamp.
- Configurable tiebreak order (default: aggregate score → task success →
  policy compliance → earliest submission timestamp).
- Toggle between "best submission" and "latest submission" view
  regardless of the live `score_aggregation` setting, so organizers can
  sanity-check either way.
- Auto-refresh on a poll interval; a manual refresh button; CSV export.

### 6.3 Runtime Settings Panel
- One form control per row in the `settings` table (§2.2), each with
  type-appropriate validation — e.g. `scoring_weights` must sum to 1.0 and
  is rejected with a clear error otherwise; `competition_phase` only
  accepts valid forward transitions.
- Every save writes a `settings_audit_log` row and shows a before/after
  diff in a confirmation step before committing.
- A stronger confirmation step for changes that affect in-progress
  submissions (e.g. changing `hidden_task_count` mid-event) — require
  the admin to type a confirmation phrase for these specifically.

### 6.4 Monitoring
- Per-team tool-call log viewer, for debugging a specific team's run or
  resolving a dispute.
- System health panel: task-pool exhaustion warnings (are there enough
  unused hidden tasks left for remaining submissions), API error rate,
  average tool-call latency.

---

## 7. Reserved: Model Restriction Layer (inactive by default)

Per the current instruction, **no model restriction is enforced in this
build.** The `allowed_llm_models` setting exists in the schema (§2.2) purely
so this can be switched on later without any code changes — when the
organizer decides on a restriction policy, populate that setting with a
specific model list and wire an enforcement check into wherever teams'
agents make LLM calls. Do not build an enforcement mechanism now; just leave
the setting present and unused, defaulted to `"*"`.

---

## 8. World / Dataset Generator (an internal CLI tool, not a live service)

```
generate_world(seed)
    → customers, transactions, subscriptions, policies, documents
      (including deliberately stale/superseded duplicates with older
      updated_at values), historical_cases

generate_task(world, family, variant)
    → { input_payload, world_state_seed, ground_truth }

validate_task(task)
    → runs the internal reference solver against the task; rejects it if
      unsolvable, ambiguous, or if ground truth cannot be derived
      deterministically from the world state alone
```

Output loads into the `tasks` table as two sets: `dataset='dev'`
(`dev_task_count` rows, ships inside the mock simulator, covering all 6
SupportOps task families and all variant types multiple times each) and
`dataset='hidden'` (`hidden_task_count` rows, loaded only into the
production database, never shipped anywhere participant-visible).

**Every single task, dev or hidden, must pass `validate_task` before being
written to its dataset.** An unsolvable or ambiguous hidden task during a
live 24-hour event becomes a dispute you have no time to resolve — this
check is not optional.

---

## 9. Participant Starter Kit (GitHub repo contents)

```
starter-kit/
├── README.md                   -- registration steps, .env explanation,
│                                    how BASE_URL swap moves you from
│                                    practice → live
├── .env.example                 -- BASE_URL, BEARER_TOKEN
├── agent.py                      -- def solve(task, tools): ... stub
├── sdk/
│   └── tools_client.py             -- thin wrapper, e.g. tools.get_customer(id);
│                                       reads BASE_URL from env so switching
│                                       modes never touches this file
├── mock_simulator/
│   ├── server.py                    -- single-file FastAPI app, SQLite-backed
│   ├── dev_tasks.json                 -- the dev_task_count public tasks
│   └── requirements.txt                 -- minimal: fastapi, uvicorn only
├── example_run.py                        -- calls /task/start, invokes
│                                             solve(), calls /task/submit,
│                                             prints the result
└── requirements.txt
```

No Docker anywhere in the participant-facing flow. `python
mock_simulator/server.py` starts practice mode on `localhost:8000`. Moving
to the real event is exactly one change: update `.env`'s `BASE_URL` to the
production URL and paste in the real bearer token — `agent.py` and
`sdk/tools_client.py` never need to change.

---

## 10. Deployment

- **Production API + Admin Dashboard + PostgreSQL:** one
  `docker-compose.yml` with three services (`api`, `admin`, `postgres`).
  The `api` service can serve both the team-facing routes and the admin
  routes/UI, or they can be split into two services sharing the same
  database — either is fine, but the compose file must deploy unmodified
  to Render **and** to a self-hosted machine, since both are on the table.
- **Mock simulator:** deliberately kept outside Docker entirely, per §9 —
  a participant with no Docker installed must still be able to run it.
- `GET /health` endpoint for uptime checks.
- Structured JSON logging from day one — needed for same-day dispute
  resolution during the live event, not something to retrofit later.

---

## 11. Non-Functional Requirements

- Correctly handle 50–100 teams making concurrent tool calls with no
  shared mutable state between them — every team's
  `task_assignments.world_runtime_state` is an isolated row, never a
  process-global object.
- Target p95 latency under 500ms for tool endpoints — these are simple
  DB read/write operations; there is no justification for them being slow.
- Bearer tokens are stored hashed and never appear in plaintext in any
  log, including `tool_call_logs.request_payload` (the token lives only
  in the auth header, which is never logged).
- No settings change (§2.2) may require a redeploy — this is a hard
  requirement, not a nice-to-have, given the one-week build window and
  likely mid-week requirement changes.

---

## 12. Phased Build Plan

### Phase 0 — Foundations
- PostgreSQL schema (§3) and migrations.
- Settings service: read, write, validate, audit-log — seeded from a
  `settings_defaults.json` file. Build this before anything else reads a
  config value, so nothing downstream ends up hardcoding a number.
- Bearer token auth middleware (hash + verify + `token_version` check).

### Phase 1 — World & Dataset Generator
- Entity generators: customers, transactions, policies, documents
  (including stale duplicates), historical cases.
- Task generator across all 6 SupportOps families × all variant types.
- Reference solver + `validate_task`.
- Generate and load the `dev` and `hidden` datasets, with counts pulled
  from `settings`, never hardcoded in the generator script itself.

### Phase 2 — Core Tool API (shared route layer)
- Implement all 6 read tools and 4 action tools (§4.1–4.2).
- Enforcement logic inside the action tools, calling the exact same
  eligibility functions the Phase 1 reference solver uses — one shared
  implementation, not a duplicate.
- Wire the `REVEAL_GROUND_TRUTH` flag through from config.
- Tool-call logging to `tool_call_logs` on every call.

### Phase 3 — Task Flow & Submission Lifecycle
- `/task/start`, `/task/submit`, `/submission/start|status|finalize`.
- Enforce `submission_limit_per_team` from settings.

### Phase 4 — Scoring Engine
- Batch grader covering all 7 dimensions (§5), weights pulled from
  `settings.scoring_weights`.
- Aggregation logic (`best`/`last`/`average`) computed from permanently
  stored per-submission scores, recomputable on demand.

### Phase 5 — Admin Dashboard
- Team CRUD, bulk import, token regeneration (§6.1).
- Leaderboard with configurable tiebreaks (§6.2).
- Settings panel wired directly to the Phase 0 settings service (§6.3).
- Tool-call log viewer and system health panel (§6.4).

### Phase 6 — Mock Simulator & Starter Kit
- Extract the shared route layer from Phase 2 into the standalone mock
  server (§9), pointed at the `dev` dataset with
  `REVEAL_GROUND_TRUTH=true`.
- Write `agent.py`, `sdk/tools_client.py`, `example_run.py`, README.

### Phase 7 — Validation & Dry Run
- Run the Phase 1 reference solver end to end through the real stack
  (mock, then production) exactly as a participant would, and confirm
  scores are sane on both a known-good and a deliberately-naive agent.
- Load-test the tool endpoints at ~100 concurrent simulated teams.
- Confirm that changing a setting (e.g. `hidden_task_count`) takes effect
  immediately with no redeploy.

### Phase 8 — Deployment & Rehearsal
- Finalize `docker-compose.yml`; deploy to Render (or self-host, same
  file, no changes).
- Seed the admin account, register several test teams, and run one full
  rehearsal submission end to end before the event opens.

---

## 13. Explicitly Flexible By Design

The following are expected to change during build week or even mid-event,
and must therefore be settings, never constants in code:

- `scoring_weights` — ship the §2.2 defaults, but editable at any time.
- `hidden_task_count` / `dev_task_count` — defaults above, editable.
- `submission_limit_per_team` and `score_aggregation` — defaults `5` /
  `best`, explicitly expected to change.
- `allowed_llm_models` — reserved and inert for now (§7), but must be
  switchable to an enforced whitelist purely via settings later.

Anything in this document not explicitly labeled an infra secret (§2.1) or
a one-time schema decision should be treated as a `settings` row, not a
hardcoded value.
