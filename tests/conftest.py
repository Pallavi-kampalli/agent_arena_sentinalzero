import os
from collections.abc import AsyncGenerator

import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

# Set test environment before imports
os.environ["ENVIRONMENT"] = "staging"
os.environ["DATABASE_URL"] = "sqlite+aiosqlite:///:memory:"
os.environ["JWT_SIGNING_SECRET"] = "test-jwt-secret-key-32-chars-long-abc"

from agent_arena.api.app import create_app
from agent_arena.api.deps import get_db_session
from agent_arena.models.base import Base


@pytest_asyncio.fixture(scope="function")
async def test_engine():
    """Creates a fresh in-memory SQLite engine for each test."""
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        echo=False,
        future=True,
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    yield engine

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await engine.dispose()


@pytest_asyncio.fixture(scope="function")
async def db_session(test_engine) -> AsyncGenerator[AsyncSession, None]:
    session_factory = async_sessionmaker(test_engine, expire_on_commit=False, class_=AsyncSession)
    async with session_factory() as session:
        yield session


@pytest_asyncio.fixture(scope="function")
async def client(test_engine, db_session) -> AsyncGenerator[AsyncClient, None]:
    """Provides an AsyncClient bound to the FastAPI app with test db overrides."""
    # Override global engine and session maker in db module for middleware and routes
    import agent_arena.db as db_module

    old_engine = db_module._engine
    old_session_maker = db_module._session_maker

    session_factory = async_sessionmaker(test_engine, expire_on_commit=False, class_=AsyncSession)
    db_module._engine = test_engine
    db_module._session_maker = session_factory

    app = create_app()

    async def override_get_db():
        async with session_factory() as s:
            yield s

    app.dependency_overrides[get_db_session] = override_get_db

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac

    # Restore
    db_module._engine = old_engine
    db_module._session_maker = old_session_maker
