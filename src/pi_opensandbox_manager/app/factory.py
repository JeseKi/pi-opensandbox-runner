from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

from fastapi import Depends, FastAPI, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.openapi.docs import get_swagger_ui_html
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from ..catalog_config import CatalogConfigError, load_catalog, sync_catalog
from ..config import ManagerSettings
from ..crypto import CredentialCipher
from ..database import ManagerDatabase
from ..openapi_docs import APP_DESCRIPTION, OPENAPI_TAGS
from ..problems import ManagerProblem, problem_response
from ..security import Authenticator, ManagerBearerCredentials, Principal, bootstrap
from ..service.long_tasks import OperationExecutor, SandboxRecoveryMonitor
from .admin_routes import register_admin_routes
from .catalog_routes import register_catalog_routes
from .command_routes import register_command_routes
from .event_routes import register_event_routes
from .filesystem_routes import register_filesystem_routes
from .instance_list_routes import register_instance_list_routes
from .instance_routes import register_instance_routes
from .mcp_routes import register_mcp_routes
from .session_routes import register_session_routes
from .terminal_routes import (
    cleanup_terminals,
    mark_connected_terminals_detached,
    register_terminal_routes,
)
from .turn_routes import register_turn_routes
from .workspace_file_routes import register_workspace_file_routes
from .workspace_routes import register_workspace_routes

logger = logging.getLogger(__name__)


def create_manager_app(
    settings: ManagerSettings | None = None,
    database: ManagerDatabase | None = None,
) -> FastAPI:
    resolved = settings or ManagerSettings.from_env()
    db_control = database or ManagerDatabase(resolved)
    authenticator = Authenticator(db_control)
    cipher = CredentialCipher(resolved.credential_encryption_key)
    executor = OperationExecutor(db_control, resolved)
    recovery_monitor = SandboxRecoveryMonitor(db_control, resolved)
    stop_event = asyncio.Event()

    async def operation_loop() -> None:
        while not stop_event.is_set():
            worked = await asyncio.to_thread(executor.execute_next)
            if not worked:
                with suppress(TimeoutError):
                    await asyncio.wait_for(
                        stop_event.wait(), timeout=resolved.operation_poll_seconds
                    )

    async def terminal_cleanup_loop() -> None:
        while not stop_event.is_set():
            await cleanup_terminals(db_control, resolved, cipher)
            with suppress(TimeoutError):
                await asyncio.wait_for(stop_event.wait(), timeout=resolved.terminal_cleanup_seconds)

    async def sandbox_recovery_loop() -> None:
        while not stop_event.is_set():
            await asyncio.to_thread(recovery_monitor.check_once)
            with suppress(TimeoutError):
                await asyncio.wait_for(
                    stop_event.wait(), timeout=resolved.sandbox_healthcheck_seconds
                )

    async def catalog_reload_loop(app: FastAPI) -> None:
        failed_hash: str | None = None
        while not stop_event.is_set():
            try:
                snapshot = await asyncio.to_thread(load_catalog, resolved.catalog_path)
                if snapshot.content_hash != app.state.catalog_hash:
                    changed = await asyncio.to_thread(_sync_catalog, db_control, snapshot)
                    app.state.catalog_hash = snapshot.content_hash
                    app.state.catalog_error = None
                    logger.info("runner catalog synchronized", extra={"changed": changed})
                failed_hash = None
            except CatalogConfigError as exc:
                marker = str(exc)
                app.state.catalog_error = marker
                if marker != failed_hash:
                    logger.error("runner catalog reload failed: %s", exc)
                    failed_hash = marker
            with suppress(TimeoutError):
                await asyncio.wait_for(
                    stop_event.wait(), timeout=max(0.1, resolved.catalog_reload_seconds)
                )

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        # The initial catalog is mandatory. Do not start a Manager that cannot
        # establish its policy and model source of truth.
        snapshot = load_catalog(resolved.catalog_path)
        db_control.initialize()
        bootstrap(db_control, resolved)
        _sync_catalog(db_control, snapshot)
        app.state.catalog_hash = snapshot.content_hash
        app.state.catalog_error = None
        mark_connected_terminals_detached(db_control)
        app.state.worker_alive = True
        worker = asyncio.create_task(operation_loop())
        terminal_cleaner = asyncio.create_task(terminal_cleanup_loop())
        recovery_monitor_task = asyncio.create_task(sandbox_recovery_loop())
        catalog_reloader = asyncio.create_task(catalog_reload_loop(app))
        try:
            yield
        finally:
            stop_event.set()
            await asyncio.gather(worker, terminal_cleaner, recovery_monitor_task, catalog_reloader)
            app.state.worker_alive = False
            db_control.dispose()

    app = FastAPI(
        title="Pi Runner Manager 内部 API",
        version="1.0.0",
        summary="统一管理 Pi/OpenSandbox Runner 的内部控制面",
        description=APP_DESCRIPTION,
        lifespan=lifespan,
        openapi_url="/v1/openapi.json",
        docs_url=None,
        openapi_tags=OPENAPI_TAGS,
    )
    static_directory = Path(__file__).resolve().parent.parent / "static"
    app.mount("/assets", StaticFiles(directory=static_directory), name="assets")
    app.state.database = db_control
    app.state.worker_alive = False
    app.state.catalog_hash = None
    app.state.catalog_error = None

    @app.middleware("http")
    async def request_id(request: Request, call_next: Any) -> Response:
        value = request.headers.get("X-Request-ID") or uuid4().hex
        request.state.request_id = value[:64]
        response = cast(Response, await call_next(request))
        response.headers["X-Request-ID"] = request.state.request_id
        response.headers["Runner-Protocol-Version"] = "1"
        return response

    @app.exception_handler(ManagerProblem)
    async def manager_problem(request: Request, exc: ManagerProblem) -> JSONResponse:
        return problem_response(exc, request)

    @app.exception_handler(RequestValidationError)
    async def validation_problem(request: Request, exc: RequestValidationError) -> JSONResponse:
        return problem_response(
            ManagerProblem(
                422,
                "validation_error",
                "request validation failed",
                extra={"errors": exc.errors()},
            ),
            request,
        )

    def principal(
        credentials: ManagerBearerCredentials,
    ) -> Principal:
        token = credentials.credentials if credentials is not None else None
        return authenticator.principal(token)

    def service_principal(
        value: Principal = Depends(principal),
    ) -> Principal:
        if value.consumer_id is None:
            raise ManagerProblem(403, "consumer_required", "consumer token required")
        return value

    def admin_principal(value: Principal = Depends(principal)) -> Principal:
        if value.kind != "admin":
            raise ManagerProblem(403, "admin_required", "admin token required")
        return value

    @app.get("/healthz", include_in_schema=False)
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/v1/docs", include_in_schema=False)
    async def swagger_ui() -> HTMLResponse:
        return get_swagger_ui_html(
            openapi_url="/v1/openapi.json",
            title=f"{app.title} - Swagger UI",
            swagger_favicon_url="/assets/manager-logo.svg",
        )

    @app.get("/readyz", include_in_schema=False)
    async def ready(request: Request) -> dict[str, str]:
        if not request.app.state.worker_alive:
            raise ManagerProblem(
                503, "worker_not_ready", "operation worker is not running", retryable=True
            )
        return {"status": "ready", "protocol_version": "1"}

    register_catalog_routes(app, db_control, service_principal)
    register_instance_list_routes(app, db_control, service_principal)
    register_instance_routes(app, db_control, cipher, service_principal)
    refresh_session = register_session_routes(app, db_control, resolved, cipher, service_principal)
    register_turn_routes(app, db_control, resolved, cipher, service_principal, refresh_session)
    register_event_routes(app, db_control, resolved, cipher, service_principal)
    register_filesystem_routes(app, db_control, resolved, cipher, service_principal)
    register_workspace_routes(app, db_control, resolved, cipher, service_principal)
    register_workspace_file_routes(app, db_control, resolved, cipher, service_principal)
    register_command_routes(app, db_control, resolved, cipher, service_principal)
    register_terminal_routes(
        app,
        db_control,
        resolved,
        cipher,
        service_principal,
    )
    register_admin_routes(app, db_control, admin_principal)
    register_mcp_routes(app, db_control, resolved, cipher, admin_principal)
    return app


def _sync_catalog(database: ManagerDatabase, snapshot: Any) -> bool:
    with database.session() as db:
        return sync_catalog(db, snapshot)
