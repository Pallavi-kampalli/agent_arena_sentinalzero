# Agent Arena — SentinelZero Platform

[![Python](https://img.shields.io/badge/Python-3.11%20%7C%203.12%20%7C%203.13-blue.svg)](https://www.python.org/)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.115+-009688.svg)](https://fastapi.tiangolo.com/)
[![PostgreSQL](https://img.shields.io/badge/PostgreSQL-16--alpine-336791.svg)](https://www.postgresql.org/)
[![Docker](https://img.shields.io/badge/Docker-Compose-2496ED.svg)](https://www.docker.com/)
[![Tests](https://img.shields.io/badge/Tests-283%20Passed-brightgreen.svg)]()

**Agent Arena** is an enterprise-grade competition and evaluation platform designed to benchmark autonomous AI agents on complex, stateful cybersecurity incident response and phishing/social engineering triage tasks (**SentinelZero**).

The platform evaluates autonomous agents across **30 canonical benchmark tasks** using **9 specialized read and defensive action tools**, enforcing strict policy adherence, evidence grounding, transactional isolation, upfront task delivery with ephemeral randomized IDs, and atomic batch submission under real-time concurrency.

---

## 1. System Architecture

The authoritative local deployment model consists of a containerized FastAPI application backed by a dedicated PostgreSQL 16 database:

```text
Participant / Browser / Agent
              ↓
    http://localhost:8000
              ↓
+------------------------------------------+
|        Agent Arena API Service           |
|  - FastAPI ASGI Web Framework            |
|  - Uvicorn (Single worker: --workers 1)  |
|  - Non-root user 'appuser' (UID 10001)   |
|  - Hierarchical In-Process Locks         |
|  - Port: 8000 (0.0.0.0:8000)             |
+--------------------+---------------------+
                     |
                     | PostgreSQL (Port 5432)
                     v
+------------------------------------------+
|       PostgreSQL 16 Engine               |
|  - Image: postgres:16-alpine             |
|  - Dedicated Volume: 'postgres_data'     |
|  - Strict ACID Transactions & Row Locks  |
+------------------------------------------+
```

### Key Architectural Invariants
- **Single Worker Process (`--workers 1`)**: Serializes concurrent requests per team through hierarchical in-process mutexes and PostgreSQL row-level locks (`SELECT ... FOR UPDATE`), preventing race conditions and double mutations.
- **Direct HTTP Entrypoint**: The API is exposed directly at `http://localhost:8000`. No reverse proxies, local TLS certificates, custom CAs, or DNS modifications are required for local production-grade operation.
- **Dedicated Migration Service**: A one-shot `migration` container applies all Alembic database schema migrations before the API service starts.
- **Decoupled Health Probes**: `/health` verifies ASGI process liveness, while `/health/ready` executes a deep database connectivity check (`SELECT 1`), returning `HTTP 503` during database outages.

---

## 2. Quickstart & Deployment

### Prerequisites
- [Docker](https://www.docker.com/) 24.0+ and Docker Compose v2.20+
- [Python](https://www.python.org/) 3.11+ (for running operator and participant scripts)

### Deploying the Platform

1. **Clone the repository**:
   ```bash
   git clone <repo-url>
   cd agent_arena
   ```

2. **Configure environment variables**:
   ```bash
   cp .env.example .env
   ```

3. **Start the platform via Docker Compose**:
   ```bash
   docker compose up -d
   ```

4. **Verify container health**:
   ```bash
   docker compose ps
   ```
   Both `agent_arena_postgres` and `agent_arena_api` should display `Up (healthy)`.

5. **Verify health endpoints**:
   ```bash
   curl -f http://localhost:8000/health
   curl -f http://localhost:8000/health/ready
   ```

---

## 3. Configuration

Configuration is managed via environment variables (loaded from `.env` or container environment). Key configuration options:

| Variable | Default | Purpose | Production Requirement |
|---|---|---|---|
| `DATABASE_URL` | `postgresql+psycopg://postgres:postgrespassword@127.0.0.1:5432/agent_arena` | Primary PostgreSQL connection string | Must target PostgreSQL (SQLite is rejected) |
| `ADMIN_PANEL_SECRET` | `dev-admin-secret-key-32-chars-min-for-agent-arena` | Secret key for `/admin/*` endpoints | $\ge 32$ chars, entropy $H \ge 3.0$, no placeholders |
| `JWT_SIGNING_SECRET` | `dev-jwt-secret-key-32-chars-min-for-agent-arena` | HMAC secret for participant JWT tokens | $\ge 32$ chars, entropy $H \ge 3.0$, no placeholders |
| `ENVIRONMENT` | `staging` | Runtime environment (`staging` or `production`) | Enforces fail-closed secret checks when `production` |
| `PORT` | `8000` | HTTP port binding | Standard HTTP port |
| `CORS_ORIGINS` | `*` | Allowed CORS origins | Wildcard `*` forbidden in production |
| `ALLOWED_HOSTS` | `*` | Allowed HTTP Host headers | Wildcard `*` forbidden in production |
| `REVEAL_GROUND_TRUTH` | `false` | Oracle feedback reveal flag | Must be `false` in production |
| `LOG_LEVEL` | `INFO` | Application log verbosity | `INFO` or `DEBUG` |

---

## 4. Participant Workflow & Starter Kit

Participants receive a clean-room, self-contained starter kit located in `starter-kit/`.

### Dual-Mode Environment Setup
The starter kit connects seamlessly to both the local mock simulator and the live arena platform via `.env`:
```env
# Practice Mode (Local Mock Simulator: http://127.0.0.1:8001)
PRACTICE_ARENA_URL=http://127.0.0.1:8001
PRACTICE_BEARER_TOKEN=dev-practice-token

# Submission Mode (Live Arena Platform: http://localhost:8000)
SUBMISSION_ARENA_URL=http://localhost:8000
SUBMISSION_TEAM_ID=<team_id>
SUBMISSION_TEAM_CODE=T-10001
SUBMISSION_BEARER_TOKEN=<team_bearer_token>
```

### Running an Agent
Participants implement their reasoning logic in `agent.py -> def solve(task, tools)` and execute `main.py`:
```bash
cd starter-kit
pip install -r requirements.txt

# Mode A: Practice Mode (Local debugging with ground truth feedback)
python main.py --mode practice --once         # Single task
python main.py --mode practice --max-tasks 5  # Up to 5 tasks

# Mode B: Submission Mode (Official competition epoch)
python main.py --mode submission              # Solves all 30 tasks sequentially & submits atomically in batch
```

### Offline Practice Mode (Mock Simulator)
Participants can practice offline against the standalone mock simulator before competing against the live platform:
```bash
python starter-kit/mock_simulator/server.py --port 8001
```
The simulator hosts a live visual debugging dashboard at `http://127.0.0.1:8001/dashboard`.

---

## 5. Admin Control Plane

Platform operators manage teams, monitor tasks, view audit logs, adjust settings, and inspect live standings via the Admin Control Plane (`/admin/*`) or the interactive dashboard at `http://localhost:8000/admin/dashboard`.

All admin routes require administrative authentication:
```bash
curl -H "Authorization: Bearer <ADMIN_PANEL_SECRET>" \
  http://localhost:8000/admin/health
```

### Key Admin Capabilities
- `GET /admin/health`: System health, task pool monitoring (30 hidden benchmark tasks), and active teams count.
- `POST /admin/teams`: Register a team, allocate an incremental 5-digit team code (`T-10001`), and generate a ready-to-copy `.env` snippet.
- `POST /admin/teams/bulk-import`: Bulk import teams from CSV.
- `GET /admin/leaderboard`: Live calculated competition standings with 4-tier tiebreaker logic.
- `GET /admin/leaderboard/export`: Export official standings as RFC 4180 CSV.
- `GET /admin/settings`: View dynamic competition settings (`hidden_task_count=30`, scoring weights).
- `PUT /admin/settings/{key}`: Dynamically update settings with immutable audit logging.
- `GET /admin/submissions`: List submissions and inspect multi-dimensional score breakdowns.
- `GET /admin/dashboard`: Single-page operator UI with team registration modal, token copy modal, submission detail auditor, and clean leaderboard.

For comprehensive admin documentation, see [ADMIN_GUIDE.md](file:///docs/ADMIN_GUIDE.md).

---

## 6. Testing & Quality Assurance

### Test Suite Execution
The repository maintains a sealed test baseline of **195 tests** covering the entire platform:
```bash
uv run pytest
```

### Reverse-Order Verification
To verify test independence and ensure zero shared state or test leakage:
```bash
pytest -q -W error $(ls tests/test_*.py | sort -r)
```

### Static Code Quality
```bash
ruff check .
ruff format --check .
mypy src
```

### Adversarial Mutation Suite
Verify that all 6 adversarial mutations (isolation bypass, live settings ignore, historical score corruption, concurrency race condition, oracle leakage, token revocation bypass) are killed:
```bash
python scripts/verify_mutations.py
```

### Starter Kit Integrity & Clean-Room Packaging
Verify starter kit export freshness and packaging independence:
```bash
python scripts/export_starter_kit.py --check
python scripts/verify_starter_kit.py
```

---

## 7. Competition Rehearsal & Disaster Recovery

### Full Competition Rehearsal
Run an end-to-end competition simulation with 3 autonomous agents (Alpha/Expert, Beta/Intermediate, Gamma/Naive) across SentinelZero incident types and tools:
```bash
python scripts/rehearse_competition.py
```
This verifies:
- Strict score ordering: $\text{Score}(\text{Alpha}) > \text{Score}(\text{Beta}) > \text{Score}(\text{Gamma})$
- Correct leaderboard calculation and export
- Complete tool invocation and audit logging
- Zero ground-truth leakage

### Disaster Recovery Verification
Verify platform resilience across all 4 disaster scenarios:
```bash
python scripts/verify_recovery.py
```
1. **Stateless API Restart**: In-flight state preserved; rapid restart.
2. **Database Outage & Recovery**: Healthcheck decoupling (`/health` 200 vs `/health/ready` 503); seamless reconnection.
3. **Isolated Non-Destructive Backup & Restore**: Full database dump restored into an isolated schema, confirming 100% entity and data integrity.
4. **Complete Compose Teardown & Recreate**: `docker compose down` and `docker compose up -d` with 100% volume persistence.

For detailed operational procedures, emergency playbooks, and backup schedules, consult [OPERATOR_RUNBOOK.md](file:///docs/OPERATOR_RUNBOOK.md).

---

## 8. Architecture & Subsystems

The platform consists of the following core production subsystems:
```text
• Domain Layer          : Authoritative business rules, immutable eligibility checks, and domain schemas
• Dataset Engine        : Deterministic world generation, task synthesis, and reference solver
• API & Storage Engine  : FastAPI async tool execution layer backed by PostgreSQL 16
• Lifecycle & Locks     : Mutex-protected submission lifecycle and assignment concurrency controls
• Multi-Dim Evaluator   : Mathematical scoring engine (Task Success, Policy, Grounding, Latency)
• Admin Control Plane   : Multi-team administration, live leaderboard, and settings control plane
• Clean-Room Packaging  : Isolated participant starter kit, mock simulator, and typed Python SDK
• Adversarial Defense   : Zero-oracle leakage guards, token revocation, and state isolation
• Operations & Recovery : Docker Compose deployment, disaster recovery verification, and automated backups
```

*Note: Remote/cloud deployment (e.g. public DNS, TLS reverse proxies, Kubernetes) is intentionally deferred. The local production-grade architecture serves as the authoritative, reproducible baseline.*

---

## 9. Adapting Agent Arena for New Problem Statements

Want to use this competition engine as the foundational template for another AI benchmark, hackathon challenge, or evaluation domain (e.g., DevOps, Financial Operations, Healthcare, Legal Discovery, Cybersecurity Incident Response)?

Consult the comprehensive file-by-file adaptation guide:
📖 **[docs/ADAPTATION_GUIDE.md](file:///docs/ADAPTATION_GUIDE.md)**

This guide provides an exhaustive file-by-file blueprint detailing:
- **What to Change**: Domain data catalogs, tool definitions, task generation, scoring rules, participant output contracts, and SDK methods.
- **What NOT to Change**: Battle-tested concurrency mutexes, row-level locks, JWT authentication, token revocation, atomic batch submission pipeline, live dashboard UI, Alembic migrations, and Docker lifecycle.
- **Step-by-Step Customization Checklist**: A 10-step protocol to spin up a brand new AI benchmark arena in hours.
