# AGENT ARENA (SUPPORTOPS) — PRODUCTION OPERATOR RUNBOOK

This runbook is the authoritative, step-by-step operational guide for deploying, managing, monitoring, securing, troubleshooting, and concluding an Agent Arena SupportOps competition event.

---

## 1. System Topology & Architecture

Agent Arena SupportOps operates on a containerized, self-hosted Docker Compose architecture designed for operational resilience, transactional determinism, disaster recovery, and zero data loss.

```
                    +------------------------------------------+
                    |    Public Internet / Client Agents       |
                    |         (Port 80 / 443 with TLS)         |
                    +--------------------+---------------------+
                                         |
                                         v
                    +------------------------------------------+
                    |   Reverse Proxy (Nginx 1.27 Alpine)      |
                    |  - Port 80: HTTP -> HTTPS 301 Redirect   |
                    |  - Port 443: TLS 1.2/1.3 Termination     |
                    |  - Host: arena.localtest.me / arena.org  |
                    |  - Forwarded headers & SNI validation    |
                    +--------------------+---------------------+
                                         | (Internal docker net)
                                         v
                    +------------------------------------------+
                    |        Agent Arena API Service           |
                    |  - FastAPI (Uvicorn --workers 1)         |
                    |  - Non-root user 'appuser' (UID 10001)   |
                    |  - In-process mutexes & hierarchical     |
                    |    row locks (Team -> Sub -> Task)       |
                    |  - Port: 8000 (Internal network only)    |
                    +--------------------+---------------------+
                                         |
                                         v
                    +------------------------------------------+
                    |       PostgreSQL 16 Engine               |
                    |  - Image: postgres:16-alpine             |
                    |  - Port: 5432 (Internal network only)    |
                    |  - Persistent Named Volume:              |
                    |      'postgres_data'                     |
                    |  - Strict ACID isolation (SERIALIZABLE   |
                    |    and READ COMMITTED with FOR UPDATE)   |
                    +------------------------------------------+
```

### Key Architectural Invariants
1. **Single Worker (`--workers 1`)**: Concurrency control uses in-process asyncio locks coupled with PostgreSQL `SELECT ... FOR UPDATE` rows. Running with 1 worker ensures hierarchical mutexes remain globally serialized within the container. Note: This architecture implements robust Disaster Recovery (DR) and operational crash resilience rather than multi-worker High Availability (HA).
2. **Reverse Proxy TLS Termination**: Nginx 1.27 serves as public entrypoint, enforcing HTTPS via 301 redirects on port 80 and terminating TLS on port 443 with valid Subject Alternative Names (`arena.localtest.me`, `arena.competition.org`).
3. **Deterministic Migration Container**: The `migration` container runs `alembic upgrade head` to completion before the `api` service is permitted to start.
4. **Database Readiness Decoupling**: Liveness (`/health`) checks the web server process; deep readiness (`/health/ready`) executes `SELECT 1` against PostgreSQL and returns 503 during database outages.
5. **Volume Persistence**: PostgreSQL stores data in the external named volume `postgres_data`, surviving container destroys, image updates, and host reboots.

---

## 2. Prerequisites & Environment Configuration

### System Requirements
- OS: Linux (Ubuntu 22.04+ / Debian 12+) or Windows Server 2022+ with Docker Desktop/Engine
- Docker Engine: 24.0.0+ with Docker Compose v2 (2.20.0+)
- Memory: Minimum 4 GB RAM (8 GB recommended for 100+ concurrent teams)
- Disk: Minimum 20 GB free space for database persistence, backups, and logs
- Python: 3.11+ (for running operator scripts)

### Production Fail-Closed Rules
When `ENVIRONMENT="production"`, the platform enforces strict fail-closed startup validation in `validate_production_configuration()`:
- `ADMIN_PANEL_SECRET`: Must be $\ge 32$ characters, must possess Shannon entropy $H \ge 3.0\text{ bits/char}$, $\ge 10$ unique characters, and cannot contain forbidden keywords (`admin`, `dev`, `secret`, `test`, `default`, `password`, `change-me`, `example`).
- `JWT_SIGNING_SECRET`: Must be $\ge 32$ characters with the same entropy ($H \ge 3.0$), unique character count ($\ge 10$), and keyword restrictions.
- `DATABASE_URL`: Must be PostgreSQL (`postgresql+psycopg://...`). SQLite is forbidden.
- `CORS_ORIGINS`: Wildcard `*` is strictly rejected. Must be an explicit comma-delimited allowlist.
- `REVEAL_GROUND_TRUTH`: Must be `"false"`. Production oracle leakage is permanently blocked.

### Configuration Template (`.env`)
```bash
# PostgreSQL Connection
DATABASE_URL=postgresql+psycopg://postgres:A_Very_Strong_Postgres_Password_2026@postgres:5432/agent_arena

# Security Credentials (Must be >= 32 characters high-entropy, H >= 3.0 bits/char)
ADMIN_PANEL_SECRET=k89A_mQ7zP_SupportOps_Admin_2026_SecureKey_Production!
JWT_SIGNING_SECRET=w34T_yU9xB_SupportOps_JWT_2026_SigningSecret_Production!

# Network & Host Binding
PORT=8000
ENVIRONMENT=production
CORS_ORIGINS=https://arena.competition.org,https://admin.competition.org,https://arena.localtest.me
ALLOWED_HOSTS=arena.competition.org,arena.localtest.me,localhost,127.0.0.1

# Competition Gating
REVEAL_GROUND_TRUTH=false
```

---

## 3. Step-by-Step Deployment Guide

### Fresh Machine Deployment
```bash
# 1. Clone repository
git clone https://github.com/organization/agent_arena.git
cd agent_arena

# 2. Prepare production .env
cp .env.example .env
# Edit .env and supply genuine production secrets (>= 32 chars, H >= 3.0 bits/char)
nano .env

# 3. Generate TLS Certificates for Reverse Proxy
python scripts/generate_tls_cert.py
# Generates certs/arena.key and certs/arena.crt with SANs:
# arena.localtest.me, arena.competition.org, localhost, 127.0.0.1

# 4. Build containers
docker compose build

# 5. Launch PostgreSQL and run automated Alembic migrations
docker compose up -d postgres
# Wait for postgres healthcheck
docker compose ps

# 6. Launch the complete stack (API, Nginx Proxy, PostgreSQL)
docker compose up -d

# 7. Verify container status
docker compose ps
# Expected:
# agent_arena_postgres   Up (healthy)
# agent_arena_migration  Exited (0)
# agent_arena_api        Up (healthy)
# agent_arena_proxy      Up (ports 0.0.0.0:80->80/tcp, 0.0.0.0:443->443/tcp)
```

---

## 4. Health Checks & Continuous Monitoring

### Healthcheck Endpoints

| Endpoint | Method | Auth | Expected Status | Purpose |
|---|---|---|---|---|
| `/health` | GET | None | 200 OK | Process liveness. Returns uptime and current competition phase. |
| `/health/ready` | GET | None | 200 OK (503 on failure) | Deep database readiness. Executes `SELECT 1` against PostgreSQL. |
| `/admin/health` | GET | `X-Admin-Secret` | 200 OK | Operational metrics: active teams, submissions count, task capacity, average tool latency. |

### Diagnostic Commands
```bash
# Check process liveness via reverse proxy (verifies TLS termination)
curl -s --cacert certs/arena.crt https://arena.localtest.me/health | jq .

# Check database readiness (returns 503 if DB is disconnected)
curl -s -i --cacert certs/arena.crt https://arena.localtest.me/health/ready

# Check HTTP -> HTTPS 301 redirection
curl -I http://arena.localtest.me/health

# Check comprehensive admin operational metrics
curl -s --cacert certs/arena.crt -H "X-Admin-Secret: $ADMIN_PANEL_SECRET" https://arena.localtest.me/admin/health | jq .

# View live application logs
docker compose logs -f api

# View reverse proxy logs
docker compose logs -f proxy

# View database query logs and connection counts
docker compose logs -f postgres
```

---

## 5. Initial Seeding & Configuration

Before teams register, verify or run the administrative seeding script:

```bash
# Run admin seeding script
python scripts/seed_admin.py

# Verify current settings
curl -s --cacert certs/arena.crt -H "X-Admin-Secret: $ADMIN_PANEL_SECRET" https://arena.localtest.me/admin/settings | jq .
```

### Canonical Production Settings
- `competition_phase`: `"registration"` (advances to `"build"` when competition starts)
- `hidden_task_count`: `200`
- `time_budget_per_task_seconds`: `180`
- `submission_limit_per_team`: `5`
- `tool_call_budget_per_task`: `40`
- `rate_limit_rpm`: `120`
- `rate_limit_burst`: `30`
- `score_aggregation`: `"best"`

---

## 6. Team Registration & Token Distribution

Teams must be registered via the Admin API. Each registration produces a unique `team_id` and a Bearer `token`.

### Single Team Registration
```bash
curl -X POST --cacert certs/arena.crt https://arena.localtest.me/admin/teams \
  -H "X-Admin-Secret: $ADMIN_PANEL_SECRET" \
  -H "Content-Type: application/json" \
  -d '{
    "team_name": "CyberDynasty",
    "members": ["lead@cyberdynasty.ai", "alice@cyberdynasty.ai"]
  }'
```
Response:
```json
{
  "team_id": "7f1e948a-1a2b-4c3d-9e8f-0123456789ab",
  "team_name": "CyberDynasty",
  "token": "arena-tok-xxxx...xxxx",
  "status": "active"
}
```
*IMPORTANT: Store the token securely. Tokens are SHA-256 hashed in PostgreSQL and cannot be recovered if lost.*

### Bulk Team Import (CSV)
```bash
curl -X POST --cacert certs/arena.crt https://arena.localtest.me/admin/teams/bulk-import \
  -H "X-Admin-Secret: $ADMIN_PANEL_SECRET" \
  -H "Content-Type: application/json" \
  -d '{
    "csv_content": "team_name,members\nTeamAlpha,alpha@org.com\nTeamBeta,beta@org.com\nTeamGamma,gamma@org.com"
  }'
```

### Token Regeneration (Credential Reset)
If a team leaks their token:
```bash
curl -X POST --cacert certs/arena.crt https://arena.localtest.me/admin/teams/{team_id}/regenerate-token \
  -H "X-Admin-Secret: $ADMIN_PANEL_SECRET"
```

---

## 7. Competition Lifecycle & Execution Operations

The competition progresses through 5 strict, irreversible phases:

```
[registration]  -->  [build]  -->  [frozen]  -->  [evaluating]  -->  [results_published]
```

### 1. Phase: `registration`
- Teams can register, practice against local Mock Simulator using starter kit.
- Live API rejects task and submission starts.

### 2. Phase: `build` (Event Active)
```bash
# Transition competition to build phase
curl -X POST --cacert certs/arena.crt https://arena.localtest.me/admin/competition/phase \
  -H "X-Admin-Secret: $ADMIN_PANEL_SECRET" \
  -H "Content-Type: application/json" \
  -d '{"new_phase": "build"}'
```
- Submissions are open.
- Teams execute tasks against `POST /task/start`, invoke tools against `POST /tools/*`, and submit decisions via `POST /task/submit`.

### 3. Phase: `frozen` (Competition Window Closes)
```bash
# Freeze submissions at the deadline
curl -X POST --cacert certs/arena.crt https://arena.localtest.me/admin/competition/phase \
  -H "X-Admin-Secret: $ADMIN_PANEL_SECRET" \
  -H "Content-Type: application/json" \
  -d '{"new_phase": "frozen"}'
```
- In-progress submissions are automatically expired.
- New submissions and task starts are rejected with HTTP 409 (`COMPETITION_FROZEN`).

### 4. Phase: `evaluating`
```bash
# Transition to evaluating phase for official scoring
curl -X POST --cacert certs/arena.crt https://arena.localtest.me/admin/competition/phase \
  -H "X-Admin-Secret: $ADMIN_PANEL_SECRET" \
  -H "Content-Type: application/json" \
  -d '{"new_phase": "evaluating"}'
```
- Operator triggers scoring across all finalized submissions.
- Admin verifies leaderboard rankings.

### 5. Phase: `results_published`
```bash
curl -X POST --cacert certs/arena.crt https://arena.localtest.me/admin/competition/phase \
  -H "X-Admin-Secret: $ADMIN_PANEL_SECRET" \
  -H "Content-Type: application/json" \
  -d '{"new_phase": "results_published"}'
```
- Final results are locked and published.

---

## 8. Dynamic Settings Management (Zero Downtime)

All platform limits and budgets can be dynamically adjusted without restarting containers or interrupting ongoing tasks:

```bash
# Adjust time budget per task (e.g., extend to 240 seconds)
curl -X PUT --cacert certs/arena.crt https://arena.localtest.me/admin/settings/time_budget_per_task_seconds \
  -H "X-Admin-Secret: $ADMIN_PANEL_SECRET" \
  -H "Content-Type: application/json" \
  -d '{"value": 240}'

# Adjust submission limit per team (e.g., increase from 5 to 7)
curl -X PUT --cacert certs/arena.crt https://arena.localtest.me/admin/settings/submission_limit_per_team \
  -H "X-Admin-Secret: $ADMIN_PANEL_SECRET" \
  -H "Content-Type: application/json" \
  -d '{"value": 7}'

# Inspect settings audit log (shows who changed what and when)
curl -s --cacert certs/arena.crt -H "X-Admin-Secret: $ADMIN_PANEL_SECRET" https://arena.localtest.me/admin/settings/audit-logs | jq .
```

---

## 9. Participant Support & Troubleshooting

### Diagnostic Playbook

#### Issue A: Team receives HTTP 401 Unauthorized
1. Verify token was supplied: `Authorization: Bearer <token>`.
2. Inspect team status:
   ```bash
   curl -s --cacert certs/arena.crt -H "X-Admin-Secret: $ADMIN_PANEL_SECRET" https://arena.localtest.me/admin/teams/{team_id} | jq .status
   ```
3. If token lost, regenerate token and send via secure channel.

#### Issue B: Team receives HTTP 409 ACTIVE_SUBMISSION_EXISTS
- A previous submission is still marked `in_progress`.
- Instruct team to finalize the prior submission: `POST /submission/{submission_id}/finalize`.

#### Issue C: Team receives HTTP 409 TASK_IN_PROGRESS
- The team has an unsubmitted task whose time budget hasn't expired.
- Either submit the task via `POST /task/submit`, or wait until the time budget elapses.

#### Issue D: Distinguishing Policy Rejection from Server Error
- Server error: HTTP 500 / 503 with error JSON.
- Policy rejection: HTTP 200 with payload `{"success": false, "reason": "...", "policy_ref": "DOC-XXXX"}`. This is intended business logic.

---

## 10. Disaster Recovery & Operational Recovery Playbooks

### Playbook 1: Periodic Operational Backup
Automated pg_dump backup using `scripts/backup_db.py`:
```bash
python scripts/backup_db.py
# Saves to backups/agent_arena_backup_YYYYMMDD_HHMMSS.sql
```
*Recommendation: Configure a cron job or systemd timer to run this every 30 minutes during competition.*

### Playbook 2: Non-Destructive Isolated Verification Restore
To verify a backup without touching the live database:
```bash
# Restores into isolated 'agent_arena_restore_verify' database
python scripts/restore_db.py backups/agent_arena_backup_20260915_073153.sql
```
Verifies table counts, team counts, tasks, and audit logs.

### Playbook 3: Emergency In-Place Restore (Production Recovery)
If catastrophic database corruption occurs:
```bash
# WARNING: Overwrites live 'agent_arena' database. Requires explicit --force flag.
python scripts/restore_db.py backups/agent_arena_backup_20260915_073153.sql --target-db agent_arena --force
```

### Playbook 4: Container Crash Recovery
If the API container crashes or hangs:
```bash
# Restart API container (stateless; zero database loss)
docker compose restart api

# Verify recovery
curl -s --cacert certs/arena.crt https://arena.localtest.me/health/ready
```

---

## 11. Incident Response Runbooks

### Runbook A: DoS / Misbehaving Team
If a team is hammering the API or violating rate limits:
```bash
# Suspend team immediately (blocks all API calls)
curl -X POST --cacert certs/arena.crt https://arena.localtest.me/admin/teams/{team_id}/status \
  -H "X-Admin-Secret: $ADMIN_PANEL_SECRET" \
  -H "Content-Type: application/json" \
  -d '{"status": "suspended"}'

# Or permanently disqualify:
curl -X POST --cacert certs/arena.crt https://arena.localtest.me/admin/teams/{team_id}/status \
  -H "X-Admin-Secret: $ADMIN_PANEL_SECRET" \
  -H "Content-Type: application/json" \
  -d '{"status": "disqualified"}'
```

### Runbook B: High Latency / Connection Exhaustion
1. Check tool logs for slowest tools:
   ```bash
   curl -s --cacert certs/arena.crt -H "X-Admin-Secret: $ADMIN_PANEL_SECRET" https://arena.localtest.me/admin/monitoring/tool-logs?limit=50 | jq .
   ```
2. Check PostgreSQL connection pool:
   ```bash
   docker compose exec postgres psql -U postgres -d agent_arena -c "SELECT count(*) FROM pg_stat_activity;"
   ```

---

## 12. Competition Finalization & Leaderboard Export

At the end of the competition window:

```bash
# 1. Freeze submissions
curl -X POST --cacert certs/arena.crt https://arena.localtest.me/admin/competition/phase \
  -H "X-Admin-Secret: $ADMIN_PANEL_SECRET" \
  -H "Content-Type: application/json" \
  -d '{"new_phase": "frozen"}'

# 2. Trigger rescoring for all completed submissions
# (Or trigger score on specific submissions via POST /admin/submissions/{submission_id}/score)

# 3. Export authoritative Leaderboard
curl -s --cacert certs/arena.crt -H "X-Admin-Secret: $ADMIN_PANEL_SECRET" https://arena.localtest.me/admin/leaderboard/export > final_leaderboard.csv

# 4. View Top 10 Leaderboard
curl -s --cacert certs/arena.crt -H "X-Admin-Secret: $ADMIN_PANEL_SECRET" https://arena.localtest.me/admin/leaderboard | jq .leaderboard[:10]

# 5. Publish Final Results
curl -X POST --cacert certs/arena.crt https://arena.localtest.me/admin/competition/phase \
  -H "X-Admin-Secret: $ADMIN_PANEL_SECRET" \
  -H "Content-Type: application/json" \
  -d '{"new_phase": "results_published"}'
```

---

## 13. Post-Event Teardown & Archival Playbook

```bash
# 1. Take final comprehensive backup
python scripts/backup_db.py

# 2. Archive backups and rehearsal results
tar -czvf competition_archive_$(date +%Y%m%d).tar.gz backups/ docs/rehearsal_results.json final_leaderboard.csv

# 3. Safely stop containers
docker compose down

# NOTE: Do NOT use 'docker compose down -v' unless you want to destroy the persistent volume.
```
