from __future__ import annotations

import hmac
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated, Any

from fastapi import APIRouter, Depends, FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.openapi.docs import get_swagger_ui_html
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from ..catalog import Catalog
from ..config import Settings
from ..execd import ExecdClient
from ..journal import EventJournal
from ..rpc import SessionSupervisor
from .command_routes import register_command_routes
from .context import BridgeContext
from .file_routes import register_file_routes
from .problems import ApiProblem, problem_response
from .session_routes import register_session_routes
from .session_runtime_routes import register_session_runtime_routes

BEARER_AUTH = HTTPBearer(auto_error=False)
BearerCredentials = Annotated[HTTPAuthorizationCredentials | None, Depends(BEARER_AUTH)]


def create_app(settings: Settings | None = None) -> FastAPI:
    resolved = settings or Settings.from_env()
    catalog = Catalog(resolved.state_root / "bridge.db", resolved.pi_session_dir)
    journal = EventJournal(
        resolved.state_root / "events",
        catalog,
        segment_bytes=resolved.event_segment_bytes,
        segment_count=resolved.event_segment_count,
    )
    supervisor = SessionSupervisor(resolved, catalog, journal)
    execd = ExecdClient(resolved.execd_url)
    ctx = BridgeContext(resolved, catalog, journal, supervisor, execd)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        resolved.workspace_root.mkdir(parents=True, exist_ok=True)
        await catalog.initialize()
        await supervisor.start()
        app.state.ready = True
        try:
            yield
        finally:
            app.state.ready = False
            await supervisor.close()
            await execd.close()

    app = FastAPI(
        title="Pi OpenSandbox Runner",
        version="0.1.0",
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
    )
    app.state.settings = resolved
    app.state.catalog = catalog
    app.state.journal = journal
    app.state.supervisor = supervisor
    app.state.execd = execd
    app.state.ready = False

    original_openapi = app.openapi

    def openapi() -> dict[str, Any]:
        schema = original_openapi()
        schema["servers"] = [{"url": "."}]
        return schema

    app.openapi = openapi  # type: ignore[method-assign]

    @app.get("/docs", include_in_schema=False)
    async def docs() -> HTMLResponse:
        return get_swagger_ui_html(openapi_url="openapi.json", title=f"{app.title} - Swagger UI")

    @app.exception_handler(ApiProblem)
    async def handle_problem(request: Request, exc: ApiProblem) -> JSONResponse:
        return problem_response(exc, request)

    @app.exception_handler(RequestValidationError)
    async def handle_validation(request: Request, exc: RequestValidationError) -> JSONResponse:
        return problem_response(
            ApiProblem(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                "validation_error",
                "request validation failed",
                extra={
                    "errors": [
                        {key: str(value) if key == "ctx" else value for key, value in error.items()}
                        for error in exc.errors()
                    ]
                },
            ),
            request,
        )

    @app.get("/healthz")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/readyz")
    async def ready(request: Request) -> dict[str, str]:
        if not request.app.state.ready:
            raise ApiProblem(503, "not_ready", "bridge is not ready")
        return {"status": "ready"}

    def authenticate(credentials: BearerCredentials = None) -> None:
        if (
            credentials is None
            or credentials.scheme.lower() != "bearer"
            or not hmac.compare_digest(credentials.credentials, resolved.api_token)
        ):
            raise ApiProblem(401, "unauthorized", "a valid bearer token is required")

    router = APIRouter(prefix="/v1", dependencies=[Depends(authenticate)])
    register_session_routes(router, ctx)
    register_file_routes(router, ctx)
    register_command_routes(router, ctx)
    register_session_runtime_routes(router, ctx)
    app.include_router(router)
    return app
