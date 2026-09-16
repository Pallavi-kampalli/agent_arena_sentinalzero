# Agent Arena: Complete Problem Statement Adaptation & Customization Guide

## 1. Executive Summary & Purpose

The **Agent Arena** codebase is engineered as a production-grade, reusable **Autonomous AI Agent Competition & Evaluation Platform**. While the default implementation benchmarks customer support operations (**SupportOps**), the underlying engine is **domain-agnostic**.

The core engine provides battle-tested infrastructure:
- High-concurrency transaction serialization and PostgreSQL row-level locks
- Secure JWT team authentication with cryptographic token revocation
- Dynamic settings management and immutable audit logging
- Full admin control plane with a single-page management dashboard and RFC 4180 CSV export
- Upfront batch task delivery with ephemeral randomized IDs
- Single atomic batch submission and multi-dimensional scoring engine
- Dual-mode participant runtime (local mock practice vs live competition)
- Automated clean-room verification and mutation testing

This guide is written for engineers and problem architects who want to fork or adapt this repository to power a **completely new problem statement** (e.g., *DevOps Incident Response, Financial Fraud Auditing, Healthcare Triage, Legal Discovery, Cybersecurity Penetration Analysis, or Autonomous Code Refactoring*).

---

## 2. Architectural Blueprint: Fixed vs. Variable

When adapting the platform for a new problem statement, the system divides into two strict layers:

```text
+========================================================================+
|             VARIABLE / DOMAIN LAYER (WHAT YOU CUSTOMIZE)               |
+========================================================================+
| • Domain Data Catalogs     : src/agent_arena/data/*.json               |
| • Domain Tools & Business  : src/agent_arena/services/tool_service.py  |
| • Tool Schemas & Contracts : src/agent_arena/schemas/tools.py & task.py|
| • Dataset Compiler         : src/agent_arena/services/dataset_service.py|
| • Multi-Dim Evaluator      : src/agent_arena/scoring/dimensions/*.py   |
| • Participant SDK Client   : starter-kit/sdk/tools_client.py           |
| • Participant Agent logic  : starter-kit/agent.py                      |
| • Mock Simulator Data      : starter-kit/mock_simulator/               |
| • Domain Tests             : tests/test_action_tools.py, etc.          |
+========================================================================+
                                   ||
                                   \/
+========================================================================+
|             FIXED / ENGINE FRAMEWORK (DO NOT TOUCH!)                   |
+========================================================================+
| • Docker Compose & Alpine  : docker-compose.yml, Dockerfile            |
| • Health Probes & Lifespan : src/agent_arena/api/app.py & health.py    |
| • Auth & Token Versioning  : src/agent_arena/services/auth_service.py  |
| • Database Core Models     : Team, Submission, ToolCallLog, Setting    |
| • Submission Lifecycle     : Batch start, ephemeral IDs, atomic submit |
| • Concurrency & Mutexes    : Team/Submission asyncio.Lock & row-locks  |
| • Admin Control Plane      : Teams CRUD, settings, audit, leaderboard  |
| • Admin Dashboard UI       : Inline responsive HTML/JS/CSS dashboard   |
| • Clean-room Packaging     : scripts/verify_starter_kit.py             |
+========================================================================+
```

---

## 3. Exhaustive File-by-File Blueprint

Below is the complete file-by-file inventory of the entire repository, specifying precisely **what to change**, **what NOT to change**, and the **rationale**.

### 3.1 Data & World Catalogs (`src/agent_arena/data/`)

| File | Status | What to Change / Keep | Rationale |
|:---|:---:|:---|:---|
| `tasks.json` | **REPLACE** | Replace with your benchmark tasks (e.g. 30 tasks). Define `task_id`, `family`, `variant`, `input_payload`, and any `task_overrides`. | Represents the inquiries/prompts served to participant agents. |
| `ground_truth.json` | **REPLACE** | Replace with the authoritative answers, required actions, expected evidence IDs, and classifications for each task. | Used exclusively by the server-side scoring engine; never leaked to participants. |
| `settings_defaults.json` | **EDIT** | Update default scoring weights (must sum to 1.0) and benchmark sizes (`hidden_task_count`, `dev_task_count`). Keep system keys like `submission_limit_per_team`. | Bootstraps PostgreSQL `settings` table on first startup. |
| `customers.json`<br>`transactions.json`<br>`subscriptions.json`<br>`policies.json`<br>`previous_cases.json` | **REPLACE / RENAME** | Delete SupportOps entity catalogs. Replace with your domain's static base entities (e.g., `servers.json`, `logs.json`, `patients.json`, `regulations.json`). | Base world state catalogs loaded into memory for tool queries. |

---

### 3.2 Schemas & Data Contracts (`src/agent_arena/schemas/`)

| File | Status | What to Change / Keep | Rationale |
|:---|:---:|:---|:---|
| `tools.py` | **REPLACE** | Define Pydantic request and response models for your new domain tools (e.g. `RestartServerRequest`, `QueryMetricsResponse`). | Validates tool arguments and responses across API endpoints. |
| `task.py` | **EDIT** | Update `TaskAnswerPayload` (the final output contract returned by `agent.solve()`). Keep generic task container schemas (`TaskReadResponse`, etc.). | Establishes the contract schema participants must satisfy. |
| `submission.py` | **KEEP** | **DO NOT TOUCH.** Contains `SubmissionStartResponse`, `BatchTaskSubmitRequest`, `BatchTaskSubmitItem`. | Core submission protocol. Accepts extra fields dynamically (`extra="ignore"`). |
| `team.py` | **KEEP** | **DO NOT TOUCH.** Contains `TeamCreate`, `TeamResponse`, `TeamStatusUpdate`. | Framework-level team schema. |
| `admin.py` | **KEEP** | **DO NOT TOUCH.** Admin health, leaderboard, CSV export, and settings schemas. | Framework-level admin contract. |
| `settings.py` | **KEEP** | **DO NOT TOUCH.** Setting update and audit schemas. | Framework-level settings contract. |

---

### 3.3 Database Models (`src/agent_arena/models/`)

| File | Status | What to Change / Keep | Rationale |
|:---|:---:|:---|:---|
| `base.py` | **KEEP** | **DO NOT TOUCH.** SQLAlchemy DeclarativeBase. | Core ORM base. |
| `task.py` | **KEEP** | **DO NOT TOUCH.** `Task` and `TaskAssignment` use flexible `JSONB` columns (`input_payload`, `world_state_seed`, `ground_truth`, `output_payload`). | Flexible schema accommodates any domain payload without database migrations! |
| `submission.py` | **KEEP** | **DO NOT TOUCH.** `Submission` table with `status`, `aggregate_score`, `duration_seconds`, and JSONB `breakdown`. | Accommodates any scoring dimensions in `breakdown`. |
| `team.py` | **KEEP** | **DO NOT TOUCH.** `Team` with `team_id`, `display_id`, `team_code`, `token_version`. | Framework-level team entity. |
| `tool_log.py` | **KEEP** | **DO NOT TOUCH.** `ToolCallLog` with `tool_name`, `request_payload`, `response_payload`, `duration_ms`. | Universal tool auditing table. |
| `settings.py` | **KEEP** | **DO NOT TOUCH.** Key-value setting store with immutable audit logging. | Universal settings engine. |

---

### 3.4 Services & Business Logic (`src/agent_arena/services/`)

| File | Status | What to Change / Keep | Rationale |
|:---|:---:|:---|:---|
| `tool_service.py` | **REWRITE** | Implement your domain tool handlers (read tools and action mutations). Ensure action tools check prerequisites, log mutations to world state, and enforce business rules. | Implements the interactive domain world for participant agents. |
| `dataset_service.py` | **EDIT** | Update `load_canonical_tasks()` to load your new entity JSON files from `DATA_DIR` and merge `task_overrides`. | Compiles modular JSON files into complete task world states. |
| `task_service.py` | **KEEP** | **DO NOT TOUCH.** Manages task retrieval and tool budget counters. | Domain-agnostic task provider. |
| `submission_service.py` | **KEEP** | **DO NOT TOUCH.** Manages upfront batch delivery, ephemeral ID generation, 30-min auto-interrupts, and atomic batch submission. | Core competition lifecycle engine. |
| `auth_service.py` | **KEEP** | **DO NOT TOUCH.** JWT token issuance, verification, constant-time HMAC admin auth, and token version revocation. | Security boundary. |
| `admin_service.py` | **KEEP** | **DO NOT TOUCH.** Teams management, CSV import, dynamic settings updates, and leaderboard computation. | Universal admin operations. |
| `settings_service.py` | **KEEP** | **DO NOT TOUCH.** Cached settings lookup and audit logging. | Universal configuration engine. |

---

### 3.5 Scoring Engine (`src/agent_arena/scoring/`)

| File | Status | What to Change / Keep | Rationale |
|:---|:---:|:---|:---|
| `engine.py` | **KEEP** | **DO NOT TOUCH.** Orchestrates dimensions and computes weighted aggregate: $\sum (w_i \cdot s_i)$. | Universal mathematical scoring coordinator. |
| `service.py` | **KEEP** | **DO NOT TOUCH.** Handles submission score persistence and breakdown storage. | Domain-agnostic score recorder. |
| `dimensions/task_success.py` | **EDIT** | Define domain task success logic (e.g. correct root-cause classification, correct resolution action). | Evaluates primary task resolution accuracy. |
| `dimensions/policy.py` | **EDIT** | Define policy adherence rules (e.g. was a mutation performed without prior read or permission?). | Evaluates safety and regulatory compliance. |
| `dimensions/evidence.py` | **EDIT** | Define grounding evaluation (e.g. precision and recall of cited log/record IDs against ground truth). | Rewards factual grounding over hallucination. |
| `dimensions/robustness.py` | **EDIT / KEEP** | Penalizes tool call rejections, schema validation failures, and unhandled exceptions. | Universal agent stability metric. |
| `dimensions/calibration.py`| **EDIT / KEEP** | Compares reported agent confidence against actual correctness (Brier score or calibration curve). | Universal confidence calibration metric. |
| `dimensions/efficiency.py` | **EDIT / KEEP** | Scores tool call economy and execution speed within budget. | Universal resource efficiency metric. |
| `dimensions/communication.py`| **EDIT / KEEP** | Evaluates customer or operator response message quality. | Evaluates text output quality. |

---

### 3.6 API Routes & Web Layer (`src/agent_arena/api/`)

| File | Status | What to Change / Keep | Rationale |
|:---|:---:|:---|:---|
| `routes/tools.py` | **REWRITE** | Define HTTP routes matching your new domain tools (`router.post("/your_tool")`). Inject `ToolService`. | Exposes REST endpoints for participant tools. |
| `routes/task.py` | **KEEP** | **DO NOT TOUCH.** Endpoints for fetching single tasks and legacy task-by-task submit. | Backward compatibility & practice mode. |
| `routes/submission.py` | **KEEP** | **DO NOT TOUCH.** `POST /submission/start`, `POST /submission/{id}/submit`, `POST /submission/{id}/abort`. | The atomic batch competition protocol. |
| `routes/admin.py` | **KEEP** | **DO NOT TOUCH.** Complete admin API and inline HTML/CSS/JS operator dashboard. | Universal admin control plane. |
| `routes/team.py` | **KEEP** | **DO NOT TOUCH.** `GET /team/me` endpoint. | Self-inspection endpoint. |
| `routes/health.py` | **KEEP** | **DO NOT TOUCH.** `/health` liveness and `/health/ready` deep database probe. | Container orchestrator probes. |
| `app.py` | **KEEP** | **DO NOT TOUCH.** FastAPI factory, middleware configuration, and lifespan dataset seeding. | Application entrypoint. |
| `middleware.py` | **KEEP** | **DO NOT TOUCH.** Bearer token extraction, rate limiting, and security headers. | Universal security middleware. |
| `deps.py` | **KEEP** | **DO NOT TOUCH.** FastAPI dependency injection for DB sessions, teams, and admin users. | Universal dependency injector. |

---

### 3.7 Participant Starter Kit (`starter-kit/`)

| File | Status | What to Change / Keep | Rationale |
|:---|:---:|:---|:---|
| `sdk/tools_client.py` | **EDIT** | Expose your new domain tools on `ToolsClient` (e.g. `def restart_server(...)`). Keep `ArenaClient` unchanged! | The Python library participants use to invoke tools. |
| `agent.py` | **REWRITE** | Update docstrings and example prompt to guide participants on solving your new problem statement. Keep the `solve(task, tools)` signature! | The ONLY file participants edit during the hackathon. |
| `main.py` | **EDIT** | Update `validate_output_contract()` to validate your domain's answer dictionary. Keep the dual-mode practice/submission runner! | Orchestration harness. Dispatches tasks and handles batch submission. |
| `mock_simulator/server.py` | **EDIT** | Implement local mock handlers for your new tools with SQLite persistence and the visual debugger. | Allows participants to test locally without internet access. |
| `mock_simulator/data/` | **REPLACE** | Provide ~30 mock/development tasks with ground truth for local offline testing. | Development dataset for practice. |
| `sample_data/` | **REPLACE** | Provide sample CSV exports for participants to inspect in Excel or pandas. | Human-readable dataset preview. |
| `requirements.txt` | **EDIT** | Update any participant dependencies (e.g. `google-genai`, `openai`, `pydantic`). | Environment requirements. |
| `README.md` | **REWRITE** | Document your problem statement, tool signatures, and scoring rubric for participants. | Participant onboarding documentation. |

---

### 3.8 Infrastructure, Database & Config

| File | Status | What to Change / Keep | Rationale |
|:---|:---:|:---|:---|
| `docker-compose.yml` | **KEEP** | **DO NOT TOUCH.** Multi-container setup (PostgreSQL 16, migration container, single-worker API). | Battle-tested deployment model. |
| `Dockerfile` | **KEEP** | **DO NOT TOUCH.** Non-root `appuser`, curl healthcheck, layer caching. | Production-grade container build. |
| `alembic/` | **KEEP** | **DO NOT TOUCH.** Initial schema migration (`0001_initial_schema.py`) includes generic JSONB fields that handle any domain data! | Zero migration maintenance required. |
| `src/agent_arena/config.py` | **KEEP** | **DO NOT TOUCH.** Pydantic Settings with fail-closed production validation. | Universal environment config. |
| `src/agent_arena/db.py` | **KEEP** | **DO NOT TOUCH.** Async SQLAlchemy engine and session makers. | Database connectivity layer. |

---

### 3.9 Scripts & Verification Suite (`scripts/`)

| File | Status | What to Change / Keep | Rationale |
|:---|:---:|:---|:---|
| `export_starter_kit.py` | **EDIT** | Update file copy lists if you renamed sample data files. | Packages `starter-kit/` into a distributable zip. |
| `verify_starter_kit.py` | **KEEP** | **DO NOT TOUCH.** Clean-room test verifying the starter kit runs in an isolated directory with zero parent imports. | Ensures participants can run out-of-the-box. |
| `verify_mutations.py` | **KEEP** | **DO NOT TOUCH.** Adversarial mutation testing verifying token revocation, state isolation, and oracle protection. | Ensures platform security cannot regress. |
| `verify_recovery.py` | **KEEP** | **DO NOT TOUCH.** Verifies stateless API restarts, DB reconnection, and backup restoration. | Disaster recovery verification. |
| `rehearse_competition.py`| **EDIT** | Update mock participant agents (Alpha, Beta, Gamma) to call your new tools and verify scoring separation. | Full dry-run simulation before the event. |

---

## 4. Step-by-Step Adaptation Recipe (10 Steps)

Follow this chronological checklist to adapt Agent Arena to a new problem statement:

### Step 1: Define the Domain Contract & Tools
1. Decide on your domain entity catalogs (e.g., `nodes.json`, `incidents.json`, `logs.json`).
2. List your tools:
   - **Read Tools** (e.g., `get_metrics`, `inspect_logs`, `search_runbooks`).
   - **Action Tools** (e.g., `restart_container`, `apply_firewall_rule`, `escalate_incident`).
3. Define the participant final output contract in `src/agent_arena/schemas/task.py` and `starter-kit/main.py` (`validate_output_contract`).

### Step 2: Generate Domain Data Catalogs
1. Create your raw entity catalogs in `src/agent_arena/data/`.
2. Generate benchmark tasks in `src/agent_arena/data/tasks.json`:
   - Recommend 30–60 tasks partitioned across 4–6 task families.
   - Include realistic descriptions, ambiguous details, or noise to test agent reasoning.
3. Generate reference answers in `src/agent_arena/data/ground_truth.json`.

### Step 3: Implement Tool Logic
1. Open `src/agent_arena/schemas/tools.py` and write request/response Pydantic models.
2. Open `src/agent_arena/services/tool_service.py`:
   - Implement read queries over your entity catalogs.
   - Implement action mutations with strict validation (e.g., rate limits, invalid target IDs).
3. Open `src/agent_arena/api/routes/tools.py` and connect HTTP endpoints to `ToolService`.

### Step 4: Adapt Dataset Loader
1. In `src/agent_arena/services/dataset_service.py`, update `load_canonical_tasks()` to read your entity JSON files.
2. Ensure `world_state_seed` encapsulates all initial state required for a team's task run.

### Step 5: Configure Multi-Dimensional Scoring
1. Update `src/agent_arena/scoring/dimensions/`:
   - `task_success.py`: Compare agent resolution with ground truth.
   - `policy.py`: Audit tool execution traces against safety rules.
   - `evidence.py`: Check precision/recall of cited evidence IDs.
2. In `src/agent_arena/data/settings_defaults.json`, adjust scoring dimension weights so they sum to `1.0`.

### Step 6: Update Participant Tools SDK
1. In `starter-kit/sdk/tools_client.py`, implement methods on `ToolsClient` matching each tool endpoint on the server.
2. Ensure all tool methods automatically pass the active `X-Task-ID` header.

### Step 7: Update Participant Runtime & Starter Kit
1. In `starter-kit/agent.py`, update prompt templates and examples for your problem statement.
2. In `starter-kit/main.py`, update `validate_output_contract()`.
3. In `starter-kit/mock_simulator/server.py`, implement mock versions of your tools for offline testing.
4. Export sample CSV files to `starter-kit/sample_data/`.
5. Rewrite `starter-kit/README.md` to document your domain tools, contract, and rules.

### Step 8: Update Test Suite
1. Update `tests/test_action_tools.py`, `tests/test_read_tools.py`, and `tests/test_scoring_dimensions.py` to test your new tools and scoring rules.
2. Run the test suite:
   ```bash
   uv run pytest
   ```

### Step 9: Rebuild Containers Cleanly
1. Tear down old volumes and build fresh images:
   ```bash
   docker compose down -v
   docker compose build --no-cache
   docker compose up -d
   ```
2. Verify startup logs:
   ```bash
   docker compose logs api
   ```
   Confirm that your tasks seeded into the database automatically.

### Step 10: Run End-to-End Verification
1. Register a test team via Dashboard (`http://localhost:8000/admin/dashboard`) or `POST /admin/teams`.
2. Copy credentials to `.env`.
3. Run participant tests:
   ```bash
   uv run main.py --mode practice --once
   uv run main.py --mode submission --max-tasks 2
   ```
4. Check standings on the Admin Leaderboard.
5. Run the verification scripts:
   ```bash
   uv run python scripts/verify_starter_kit.py
   uv run python scripts/verify_mutations.py
   ```

---

## 5. Summary Cheat Sheet: What Changes vs. What Stays

```text
┌─────────────────────────────────────────────────────────────────────────┐
│                           WHAT YOU CHANGE                               │
├─────────────────────────────────────────────────────────────────────────┤
│ 1. src/agent_arena/data/*.json              (Your problem data)         │
│ 2. src/agent_arena/schemas/tools.py         (Your tool request/response)│
│ 3. src/agent_arena/schemas/task.py          (Your agent answer contract)│
│ 4. src/agent_arena/services/tool_service.py (Your tool logic & actions) │
│ 5. src/agent_arena/api/routes/tools.py      (Your tool HTTP endpoints)  │
│ 6. src/agent_arena/scoring/dimensions/*.py  (Your grading criteria)     │
│ 7. starter-kit/sdk/tools_client.py          (Your client SDK methods)   │
│ 8. starter-kit/agent.py                     (Your agent starter code)   │
│ 9. starter-kit/mock_simulator/              (Your offline practice tool)│
│ 10. starter-kit/sample_data/                (Your sample CSV files)     │
└─────────────────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────────────────┐
│                        WHAT YOU NEVER CHANGE                            │
├─────────────────────────────────────────────────────────────────────────┤
│ 1. docker-compose.yml & Dockerfile          (Production infra setup)    │
│ 2. alembic/versions/0001_initial_schema.py  (Generic JSONB ORM tables)  │
│ 3. src/agent_arena/models/*                 (Database architecture)     │
│ 4. src/agent_arena/services/auth_service.py (JWT security & revocation) │
│ 5. src/agent_arena/services/submission_*.py (Batch concurrency engine)  │
│ 6. src/agent_arena/api/routes/submission.py (Atomic submission API)     │
│ 7. src/agent_arena/api/routes/admin.py      (Admin dashboard & backend) │
│ 8. src/agent_arena/api/middleware.py        (Rate limiter & security)   │
│ 9. starter-kit/main.py                      (Execution loop & harness)  │
│ 10. scripts/verify_starter_kit.py           (Clean-room verification)   │
└─────────────────────────────────────────────────────────────────────────┘
```
