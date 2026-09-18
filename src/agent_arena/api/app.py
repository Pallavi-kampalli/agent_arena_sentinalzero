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

        try:
            import sqlalchemy as sa
            from agent_arena.models.task import Task
            from agent_arena.services.dataset_service import DatasetService

            task_count_stmt = sa.select(sa.func.count()).select_from(Task).where(Task.dataset == "hidden")
            existing_tasks = (await session.execute(task_count_stmt)).scalar() or 0
            if existing_tasks == 0:
                dataset_service = DatasetService(session)
                await dataset_service.generate_and_load_dataset(dataset_type="hidden")
                logger.info("Seeded canonical hidden tasks into database on startup")
        except Exception as e:
            logger.error(f"Failed to seed hidden tasks dataset on startup: {e}")

    yield
    logger.info("Shutting down Agent Arena API")


def create_app() -> FastAPI:
    app = FastAPI(
        title="Agent Arena — SentinelZero Platform",
        version="0.1.0",
        description="Production API & Evaluation Engine for SentinelZero Security Incident Response Competition",
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

    from fastapi.responses import RedirectResponse

    @app.get("/", include_in_schema=False)
    @app.get("/admin", include_in_schema=False)
    @app.get("/admin/", include_in_schema=False)
    @app.get("/dashboard", include_in_schema=False)
    @app.get("/admin/dasboard", include_in_schema=False)
    async def redirect_to_admin_dashboard():
        """Redirect browser root and convenience URLs directly to Admin Dashboard UI."""
        return RedirectResponse(url="/admin/dashboard", status_code=307)

    return app


app = create_app()
