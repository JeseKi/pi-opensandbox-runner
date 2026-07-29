from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from typing import Any, cast
from uuid import uuid4

from fastapi import Depends, FastAPI, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from ..config import ManagerSettings
from ..crypto import CredentialCipher
from ..database import ManagerDatabase
from ..openapi_docs import APP_DESCRIPTION, OPENAPI_TAGS
from ..problems import ManagerProblem, problem_response
from ..security import Authenticator, ManagerBearerCredentials, Principal, bootstrap
from ..service.long_tasks import OperationExecutor
from ..service.short_transactions import seed_catalog
from .admin_routes import register_admin_routes
from .catalog_routes import register_catalog_routes
from .command_routes import register_command_routes
from .event_routes import register_event_routes
from .instance_list_routes import register_instance_list_routes
from .instance_routes import register_instance_routes
from .session_routes import register_session_routes
from .terminal_routes import (
    cleanup_terminals,
    mark_connected_terminals_detached,
    register_terminal_routes,
)
from .turn_routes import register_turn_routes
from .workspace_routes import register_workspace_routes


def create_manager_app(
    settings: ManagerSettings | None = None,
    database: ManagerDatabase | None = None,
) -> FastAPI:
    resolved = settings or ManagerSettings.from_env()
    db_control = database or ManagerDatabase(resolved)
    authenticator = Authenticator(db_control)
    cipher = CredentialCipher(resolved.credential_encryption_key)
    executor = OperationExecutor(db_control, resolved)
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
                await asyncio.wait_for(
                    stop_event.wait(), timeout=resolved.terminal_cleanup_seconds
                )

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        db_control.initialize()
        bootstrap(db_control, resolved)
        with db_control.session() as db:
            seed_catalog(db)
        mark_connected_terminals_detached(db_control)
        app.state.worker_alive = True
        worker = asyncio.create_task(operation_loop())
        terminal_cleaner = asyncio.create_task(terminal_cleanup_loop())
        try:
            yield
        finally:
            stop_event.set()
            await asyncio.gather(worker, terminal_cleaner)
            app.state.worker_alive = False
            db_control.dispose()

    app = FastAPI(
        title="Pi Runner Manager 内部 API",
        version="1.0.0",
        summary="统一管理 Pi/OpenSandbox Runner 的内部控制面",
        description=APP_DESCRIPTION,
        lifespan=lifespan,
        openapi_url="/v1/openapi.json",
        docs_url="/v1/docs",
        openapi_tags=OPENAPI_TAGS,
    )
    app.state.database = db_control
    app.state.worker_alive = False

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
    refresh_session = register_session_routes(
        app, db_control, resolved, cipher, service_principal
    )
    register_turn_routes(
        app, db_control, resolved, cipher, service_principal, refresh_session
    )
    register_event_routes(app, db_control, resolved, cipher, service_principal)
    register_workspace_routes(app, db_control, resolved, cipher, service_principal)
    register_command_routes(app, db_control, resolved, cipher, service_principal)
    register_terminal_routes(
        app,
        db_control,
        resolved,
        cipher,
        service_principal,
    )
    register_admin_routes(app, db_control, admin_principal)
    return app
