import sys

if sys.platform == "win32":
    import asyncio

    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

from collections.abc import AsyncGenerator
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from agent_arena.config import get_config
from agent_arena.models.base import Base

_engine = None
_session_maker = None


def get_engine(database_url: str | None = None):
    global _engine, _session_maker
    if _engine is None or database_url is not None:
        url = database_url or get_config().DATABASE_URL
        # Set connect_args / pool settings based on engine dialect
        is_sqlite = "sqlite" in url
        engine_kwargs: dict[str, Any] = {"echo": False, "future": True}
        if not is_sqlite:
            engine_kwargs.update(
                {
                    "pool_size": 20,
                    "max_overflow": 10,
                    "pool_pre_ping": True,
                }
            )
        _engine = create_async_engine(url, **engine_kwargs)
        _session_maker = async_sessionmaker(_engine, expire_on_commit=False, class_=AsyncSession)
    return _engine


def get_session_maker(database_url: str | None = None) -> async_sessionmaker[AsyncSession]:
    global _session_maker
    if _session_maker is None or database_url is not None:
        get_engine(database_url)
    assert _session_maker is not None
    return _session_maker


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    session_factory = get_session_maker()
    async with session_factory() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise


async def create_tables(engine=None) -> None:
    eng = engine or get_engine()
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def drop_tables(engine=None) -> None:
    eng = engine or get_engine()
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
