from typing import Callable
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from agent_arena.db import get_session_maker
from agent_arena.logging import logger
from agent_arena.services.auth_service import authenticate_bearer_token

PUBLIC_PATHS = {
    "/health",
    "/docs",
    "/redoc",
    "/openapi.json",
}

PUBLIC_PREFIXES = (
    "/health",
    "/docs",
    "/redoc",
    "/openapi.json",
    "/admin",  # Admin panel has its own auth mechanism (PRD §6)
)


class BearerAuthMiddleware(BaseHTTPMiddleware):
    """Middleware enforcing Bearer token authentication per PRD §4, §11, and §12.
    
    Extracts Bearer token, validates signature, verifies token_version against the database,
    checks that team is active, and binds the authenticated team to request.state.
    """

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        path = request.url.path

        # Bypass public endpoints
        if path in PUBLIC_PATHS or any(path.startswith(prefix) for prefix in PUBLIC_PREFIXES):
            return await call_next(request)

        auth_header = request.headers.get("Authorization")
        if not auth_header or not auth_header.startswith("Bearer "):
            return JSONResponse(
                status_code=401,
                content={
                    "error": "UNAUTHORIZED",
                    "detail": "Missing or malformed Authorization header. Expected 'Bearer <token>'.",
                },
            )

        token = auth_header.removeprefix("Bearer ").strip()
        if not token:
            return JSONResponse(
                status_code=401,
                content={
                    "error": "UNAUTHORIZED",
                    "detail": "Bearer token string is empty.",
                },
            )

        # Authenticate token against DB
        session_maker = get_session_maker()
        async with session_maker() as session:
            try:
                team = await authenticate_bearer_token(session, token)
            except ValueError as e:
                err_code = str(e)
                if err_code == "TOKEN_EXPIRED":
                    return JSONResponse(
                        status_code=401,
                        content={"error": "TOKEN_EXPIRED", "detail": "Bearer token has expired."},
                    )
                elif err_code.startswith("TEAM_STATUS_"):
                    status_name = err_code.removeprefix("TEAM_STATUS_").lower()
                    return JSONResponse(
                        status_code=403,
                        content={"error": "FORBIDDEN", "detail": f"Team status is '{status_name}'."},
                    )
                else:
                    return JSONResponse(
                        status_code=401,
                        content={"error": "UNAUTHORIZED", "detail": f"Authentication failed: {err_code}"},
                    )

            # Bind authenticated team info to request.state
            request.state.team = team
            request.state.team_id = team.team_id

        return await call_next(request)
