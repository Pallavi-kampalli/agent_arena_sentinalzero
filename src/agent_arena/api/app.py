from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from agent_arena.api.middleware import BearerAuthMiddleware
from agent_arena.api.routes.admin import router as admin_router
from agent_arena.api.routes.health import router as health_router
from agent_arena.api.routes.submission import router as submission_router
from agent_arena.api.routes.task import router as task_router
from agent_arena.api.routes.team import router as team_router
from agent_arena.api.routes.tools import router as tools_router
from agent_arena.config import get_config
from agent_arena.db import get_session_maker
from agent_arena.logging import logger, setup_logging
from agent_arena.services.settings_service import SettingsService


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup: setup logging, seed default settings
    config = get_config()
    setup_logging()
    logger.info(f"Starting Agent Arena API in {config.ENVIRONMENT} mode")

    session_maker = get_session_maker()
    async with session_maker() as session:
        settings_service = SettingsService(session)
        try:
            await settings_service.seed_defaults()
        except Exception as e:
            logger.error(f"Failed to seed default settings on startup: {e}")

    yield
    logger.info("Shutting down Agent Arena API")


def create_app() -> FastAPI:
    app = FastAPI(
        title="Agent Arena — SupportOps Platform",
        version="0.1.0",
        description="Production API & Evaluation Engine for SupportOps Competition",
        lifespan=lifespan,
    )

    config = get_config()

    # CORS configuration
    app.add_middleware(
        CORSMiddleware,
        allow_origins=config.cors_origins_list,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # Bearer Token Auth Middleware (PRD §4, §11, §12)
    app.add_middleware(BearerAuthMiddleware)

    # Mount routers
    app.include_router(health_router)
    app.include_router(team_router)
    app.include_router(tools_router)
    app.include_router(submission_router)
    app.include_router(task_router)
    app.include_router(admin_router)

    return app


app = create_app()
