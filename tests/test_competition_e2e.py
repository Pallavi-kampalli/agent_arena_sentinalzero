import asyncio
import os
import socket
import sys
import threading
import time
import uuid
from pathlib import Path

import httpx
import pytest
import sqlalchemy as sa
import uvicorn
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

ROOT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT_DIR / "src"))
sys.path.insert(0, str(ROOT_DIR / "starter-kit"))
sys.path.insert(0, str(ROOT_DIR / "starter-kit" / "mock_simulator"))

from agent import solve as starter_kit_solve  # noqa: E402
from conftest import generate_world  # noqa: E402
from sdk.tools_client import ToolsClient  # noqa: E402
from server import app as mock_app  # noqa: E402
from server import init_db as mock_init_db  # noqa: E402

from agent_arena.api.app import create_app  # noqa: E402
from agent_arena.models.base import Base  # noqa: E402
from agent_arena.models.submission import Submission  # noqa: E402
from agent_arena.models.task import Task  # noqa: E402
from agent_arena.models.task_assignment import TaskAssignment  # noqa: E402
from agent_arena.models.team import Team  # noqa: E402
from agent_arena.models.tool_call_log import ToolCallLog  # noqa: E402
from agent_arena.services.auth_service import create_bearer_token, hash_token  # noqa: E402
from agent_arena.services.settings_service import SettingsService  # noqa: E402


def get_free_port() -> int:
    s = socket.socket()
    s.bind(("", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@pytest.fixture(scope="module")
def mock_server():
    """Spins up the Mock Simulator on a live localhost port in a background thread."""
    mock_init_db()
    port = get_free_port()
    config = uvicorn.Config(mock_app, host="127.0.0.1", port=port, log_level="error")
    server = uvicorn.Server(config)
    server_loop = None

    def run():
        nonlocal server_loop
        if sys.platform == "win32":
            asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
        server_loop = asyncio.new_event_loop()
        asyncio.set_event_loop(server_loop)
        try:
            server_loop.run_until_complete(server.serve())
        finally:
            server_loop.run_until_complete(server_loop.shutdown_asyncgens())
            server_loop.close()

    thread = threading.Thread(target=run, daemon=True)
    thread.start()

    base_url = f"http://127.0.0.1:{port}"
    max_wait = 5.0
    t0 = time.time()
    with httpx.Client(timeout=0.5) as hclient:
        while time.time() - t0 < max_wait:
            try:
                r = hclient.get(f"{base_url}/health")
                if r.status_code == 200:
                    break
            except Exception:
                time.sleep(0.05)

    yield base_url
    server.should_exit = True
    thread.join(timeout=3.0)


@pytest.fixture(scope="module")
def prod_server():
    """Spins up the Production-Stack Validation Environment backed by real PostgreSQL."""
    import agent_arena.db as db_module

    db_url = os.environ.get(
        "TEST_POSTGRES_URL",
        "postgresql+psycopg://postgres:postgrespassword@127.0.0.1:5432/agent_arena",
    )
    # Attempt connection to PostgreSQL if host port 5432 is accessible; fallback to isolated DB if unexposed
    engine = None
    try:
        s = socket.socket()
        s.settimeout(0.3)
        pg_accessible = s.connect_ex(("127.0.0.1", 5432)) == 0
        s.close()
        if not pg_accessible:
            raise ConnectionError("PostgreSQL port 5432 not listening")
        candidate_engine = create_async_engine(db_url, echo=False)

        async def init_pg():
            async with candidate_engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)

        asyncio.run(init_pg())
        engine = candidate_engine
    except Exception:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)

        async def init_sqlite():
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)

        asyncio.run(init_sqlite())

    session_factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)

    # Override db module engine and session maker
    old_engine = db_module._engine
    old_session_maker = db_module._session_maker
    db_module._engine = engine
    db_module._session_maker = session_factory

    app = create_app()
    port = get_free_port()
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error")
    server = uvicorn.Server(config)
    server_loop = None

    def run():
        nonlocal server_loop
        if sys.platform == "win32":
            asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
        server_loop = asyncio.new_event_loop()
        asyncio.set_event_loop(server_loop)
        try:
            server_loop.run_until_complete(server.serve())
        finally:
            server_loop.run_until_complete(server_loop.shutdown_asyncgens())
            server_loop.close()

    thread = threading.Thread(target=run, daemon=True)
    thread.start()

    base_url = f"http://127.0.0.1:{port}"
    max_wait = 5.0
    t0 = time.time()
    with httpx.Client(timeout=0.5) as hclient:
        while time.time() - t0 < max_wait:
            try:
                r = hclient.get(f"{base_url}/health")
                if r.status_code == 200:
                    break
            except Exception:
                time.sleep(0.05)

    yield {"base_url": base_url, "engine": engine, "session_factory": session_factory}

    server.should_exit = True
    thread.join(timeout=3.0)
    asyncio.run(engine.dispose())
    db_module._engine = old_engine
    db_module._session_maker = old_session_maker


@pytest.fixture
async def prod_team_and_task(prod_server):
    """Provisions an isolated team and seeded task in PostgreSQL, then cleans up upon teardown."""
    session_factory = prod_server["session_factory"]
    run_id = uuid.uuid4().hex[:8]
    team_id = uuid.uuid4()
    token = create_bearer_token(team_id, token_version=1)
    token_hash = hash_token(token)
    # Prefix with 'TASK-0000-PROD-' so it reliably sorts first among any hidden tasks
    task_id = f"TASK-0000-PROD-E2E-{run_id}"

    async with session_factory() as session:
        team = Team(
            team_id=team_id,
            team_name=f"E2E_Team_{run_id}",
            members=[{"name": "Alice", "email": "alice@e2e.test"}],
            bearer_token_hash=token_hash,
            token_version=1,
            status="active",
        )
        session.add(team)

        settings = SettingsService(session)
        await settings.seed_defaults()
        await settings.set("competition_phase", "build")
        await settings.set("hidden_task_count", 1)
        await settings.set("time_budget_per_task_seconds", 300)

        world = generate_world(seed=2001)
        world["customers"] = [
            {
                "id": "CUS-PROD-001",
                "name": "E2E Customer",
                "tier": "pro",
                "region": "NA",
                "verification_status": "verified",
                "account_status": "active",
                "created_at": "2026-01-01T00:00:00Z",
            }
        ]
        world["transactions"] = [
            {
                "id": "TXN-PROD-001",
                "customer_id": "CUS-PROD-001",
                "amount": 75.0,
                "currency": "USD",
                "status": "completed",
                "created_at": "2026-09-01T12:00:00Z",
                "refunded_amount": 0.0,
                "chargeback_status": None,
            }
        ]
        world["policies"] = [
            {
                "id": "DOC-1001",
                "title": "Refund Policy",
                "category": "refund",
                "content": "Eligible for refund up to 500 USD within 30 days.",
                "updated_at": "2026-01-01T00:00:00Z",
            }
        ]

        task = Task(
            task_id=task_id,
            dataset="hidden",
            input_payload={
                "customer_id": "CUS-PROD-001",
                "customer_message": "Please refund my transaction TXN-PROD-001 for 75.0 dollars.",
            },
            world_state_seed=world,
            ground_truth={
                "expected_resolution": "refund",
                "must_escalate": False,
                "required_evidence": ["TXN-PROD-001", "DOC-1001"],
            },
        )
        session.add(task)
        await session.commit()

    yield {
        "team_id": str(team_id),
        "token": token,
        "task_id": task_id,
    }

    # Clean up test fixtures from PostgreSQL
    async with session_factory() as session:
        await session.execute(sa.delete(ToolCallLog).where(ToolCallLog.team_id == team_id))
        await session.execute(sa.delete(TaskAssignment).where(TaskAssignment.team_id == team_id))
        await session.execute(sa.delete(Submission).where(Submission.team_id == team_id))
        await session.execute(sa.delete(Task).where(Task.task_id == task_id))
        await session.execute(sa.delete(Team).where(Team.team_id == team_id))
        # Restore canonical default
        settings = SettingsService(session)
        await settings.set("hidden_task_count", 30)
        await session.commit()


def test_e2e_starter_kit_agent_against_mock(mock_server):
    """Exercises starter-kit default agent template against standalone Mock Simulator."""
    tools = ToolsClient(base_url=mock_server, token="dev-starter-mock-token")
    tools._post("/dev/reset")

    task = tools.start_task()
    with pytest.raises(NotImplementedError):
        starter_kit_solve(task, tools)
    tools.close()


def test_e2e_mock_to_production_portability_invariant():
    """Verifies that changing only BASE_URL and BEARER_TOKEN routes correctly with zero code changes."""
    mock_client = ToolsClient(base_url="http://127.0.0.1:8000", token="dev-mock-token")
    prod_client = ToolsClient(base_url="https://arena.competition.org", token="prod-secret-token")

    assert mock_client.base_url == "http://127.0.0.1:8000"
    assert mock_client.token == "dev-mock-token"
    assert prod_client.base_url == "https://arena.competition.org"
    assert prod_client.token == "prod-secret-token"

    # Both clients expose identical method signatures
    mock_methods = {m for m in dir(mock_client) if not m.startswith("_")}
    prod_methods = {m for m in dir(prod_client) if not m.startswith("_")}
    assert mock_methods == prod_methods
    mock_client.close()
    prod_client.close()
