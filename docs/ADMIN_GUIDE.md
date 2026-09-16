# Agent Arena — Admin Control Plane Guide

## 1. Overview & Architectural Role

The **Admin Control Plane** (`/admin/*`) provides competition organizers and platform operators with centralized, real-time control over the Agent Arena — SupportOps platform.

The admin control plane is strictly layered on top of the core platform services (domain models, solver engine, database models, and scoring system):
- **Zero Core Tampering:** Participant contracts (`/task/*`, `/submission/*`, `/tools/*`) and canonical scoring mathematics remain completely untouched.
- **Unified Service Architecture:** HTTP routes delegate to `AdminService`, which orchestrates canonical `AuthService`, `SettingsService`, and `ScoringService`.
- **Atomic Operations:** Team mutations utilize shared in-process `asyncio.Lock` wrappers and database transactions to eliminate race conditions.
- **Comprehensive Audit Trail:** All administrative parameter modifications, status changes, and token revocations are persisted immutably in `settings_audit_log`.

---

## 2. Authentication & Security Boundary

Admin endpoints are secured independently from participant JWTs:
- **Shared Secret Scheme:** Verified via `hmac.compare_digest` against `config.ADMIN_PANEL_SECRET` in constant time.
- **Supported Headers:**
  - `X-Admin-Secret: <ADMIN_PANEL_SECRET>`
  - `Authorization: Bearer <ADMIN_PANEL_SECRET>`
- **Participant Isolation:** Participant JWT bearer tokens are strictly rejected on all admin routes (returning HTTP 401 Unauthorized `ADMIN_UNAUTHORIZED`).
- **Credential Protection:** Responses from admin endpoints never return database password hashes, token hashes, or HMAC salts. Raw bearer tokens are presented only once upon initial creation or regeneration.

---

## 3. API Reference

### 3.1 Overview & System Health
- **`GET /admin/health`**:
  Returns operational status including database connectivity, total registered teams, active submissions count, hidden benchmark tasks pool size, and task pool exhaustion alerts.
- **`GET /admin/dashboard`**:
  Lightweight, single-page responsive operational dashboard UI for operators, featuring live metrics, team controls, and log inspect modals.

### 3.2 Team Management
- **`POST /admin/teams`**:
  Registers an individual team. Returns `team_id`, initial `token_version`, raw `token`, and a ready-to-use `.env` snippet formatted with `AGENT_ARENA_BASE_URL`, `AGENT_ARENA_TEAM_ID`, and `AGENT_ARENA_BEARER_TOKEN`.
- **`POST /admin/teams/bulk-import`**:
  Accepts CSV data (required header: `team_name`, optional: `member_names`, `member_emails`, `github_repo_url`). Automatically creates teams, safely skips pre-existing names, and returns generated credentials.
- **`GET /admin/teams`**:
  Lists teams with pagination (`limit`, `offset`), status filtering (`active`, `suspended`, `disqualified`), and substring search query (`q` / `search`).
- **`GET /admin/teams/{team_id}`**:
  Returns comprehensive team metadata and chronological submission history.
- **`PATCH /admin/teams/{team_id}`**:
  Updates team display name, member profiles, or GitHub repository URL.
- **`PUT /admin/teams/{team_id}/status`** (also `POST`):
  Updates status to `active`, `suspended`, or `disqualified`. Suspended and disqualified teams are immediately barred from participant tool execution and task starts.
- **`POST /admin/teams/{team_id}/token`** (also `/regenerate-token`):
  Atomically increments `token_version` and generates a fresh JWT bearer token. Previous tokens are immediately rejected by middleware upon their next request.

### 3.3 Dynamic Settings & Competition State Machine
- **`GET /admin/settings`**:
  Returns merged configuration settings, defaults, and last-updated timestamps.
- **`PUT /admin/settings/{key}`**:
  Updates a configuration parameter. Normalization checks enforce that `scoring_weights` must sum to 1.0. Writes an entry to the audit log.
- **`GET /admin/settings/audit-logs`** (also `/audit-log`):
  Queries immutable audit history with optional `key` filtering and pagination.
- **`GET /admin/competition/phase`**:
  Reports current phase and permitted forward transitions.
- **`POST /admin/competition/phase`**:
  Enforces monotonic forward progression across competition phases:
  `registration -> build -> frozen -> evaluating -> results_published`.
  Backward transitions are strictly rejected with HTTP 400 `INVALID_PHASE_TRANSITION`.

### 3.4 Live Leaderboard & Dispute Resolution
- **`GET /admin/leaderboard`**:
  Calculates live standings supporting `best` (highest aggregate score) and `latest` (most recent attempt) modes.
  Applies the canonical 4-tier tiebreaker hierarchy:
  1. `aggregate_score` (Descending)
  2. `task_success` (Descending)
  3. `policy` compliance (Descending)
  4. `completed_at` (Ascending, earliest completion wins)
  *Disqualified teams are strictly excluded from the leaderboard.*
- **`GET /admin/leaderboard/export`**:
  Generates an RFC 4180 compliant CSV export of the live standings.
- **`GET /admin/submissions`**:
  Lists submissions with optional `team_id` and `status` filters.
- **`GET /admin/submissions/{submission_id}`**:
  Inspects submission score breakdown and per-task audit results.
- **`POST /admin/submissions/{submission_id}/score`**:
  Triggers scoring for finalized submissions via `ScoringService`. Supports `force=true` to force a rescore if competition weights were adjusted.
- **`GET /admin/tool-logs`** (also `/monitoring/tool-logs`):
  Queries granular tool execution records filtered by `team_id`, `task_id`, `tool_name`, and `rejections_only`.

---

## 4. Operational Runbook

### Handling Disqualifications
1. Navigate to the team in `GET /admin/teams` or Dashboard.
2. Send `PUT /admin/teams/{id}/status` with `{"status": "disqualified"}`.
3. The platform immediately cuts off tool access and removes the team from the leaderboard.

### Emergency Token Revocation
1. Send `POST /admin/teams/{id}/token`.
2. The team's `token_version` is bumped from $V$ to $V+1$.
3. All requests signed with token version $V$ fail authentication immediately with `TOKEN_REVOKED`.
4. Distribute the new `env_snippet` securely to the team lead.

### Adjusting Scoring Weights
1. Ensure weights sum to exactly 1.0.
2. Send `PUT /admin/settings/scoring_weights` with the updated JSON dictionary.
3. Call `POST /admin/submissions/{id}/score?force=true` to recalculate any completed submissions with the new weights.
