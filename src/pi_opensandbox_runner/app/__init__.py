from __future__ import annotations

import ipaddress
import socket
import time
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
from ..model_catalog import ModelCatalog
from ..rpc import SessionSupervisor
from .command_routes import register_command_routes
from .context import BridgeContext
from .file_routes import register_file_routes
from .model_routes import register_model_routes
from .problems import ApiProblem, problem_response
from .session_routes import register_session_routes
from .session_runtime_routes import register_session_runtime_routes
from .terminal_routes import register_terminal_routes
from .workspace_file_routes import register_workspace_file_routes

EXTERNAL_BEARER_AUTH = HTTPBearer(auto_error=False)
ExternalBearerCredentials = Annotated[
    HTTPAuthorizationCredentials | None, Depends(EXTERNAL_BEARER_AUTH)
]


class TrustedProxyAddresses:
    """Resolve the controller name periodically without trusting forwarded headers."""

    def __init__(self, hostname: str, cache_seconds: float) -> None:
        self.hostname = hostname
        self.cache_seconds = cache_seconds
        self._addresses: set[str] = set()
        self._refresh_after = 0.0

    def contains(self, host: str) -> bool:
        try:
            if ipaddress.ip_address(host).is_loopback:
                return True
        except ValueError:
            return False

        now = time.monotonic()
        if now >= self._refresh_after:
            try:
                results = socket.getaddrinfo(self.hostname, None, type=socket.SOCK_STREAM)
                self._addresses = {str(item[4][0]) for item in results}
            except socket.gaierror:
                self._addresses = set()
            self._refresh_after = now + self.cache_seconds
        return host in self._addresses


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
    model_catalog = ModelCatalog(
        resolved.model_catalog_path,
        litellm_api_base=resolved.litellm_api_base,
        virtual_key=resolved.litellm_virtual_key,
    )
    ctx = BridgeContext(resolved, catalog, journal, supervisor, execd, model_catalog)
    trusted_proxy_addresses = TrustedProxyAddresses(
        resolved.trusted_proxy_host, resolved.trusted_proxy_cache_seconds
    )

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
        description=(
            "通过 HTTP 管理容器内的 Pi RPC Session、OpenSandbox 文件与命令。"
            "经 OpenSandbox server proxy 访问时，所有 `/v1` 接口均需要 "
            "`Authorization: Bearer <bridge-proxy-token>`。"
            "`cwd` 是 Pi 初始工作目录，不是权限边界。"
        ),
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_tags=[
            {"name": "Health", "description": "Bridge 存活与就绪状态。"},
            {"name": "Sessions", "description": "Pi Session 的创建、配置、历史与 prompt。"},
            {"name": "Session runtime", "description": "运行中的 Pi 控制、上下文和 SSE 事件。"},
            {"name": "Models", "description": "Pi LiteLLM 模型目录管理。"},
            {"name": "Files", "description": "通过 OpenSandbox Execd 浏览和修改容器文件。"},
            {"name": "Workspace Files", "description": "受限于 /root/workspace 的用户文件管理。"},
            {"name": "Commands", "description": "通过 OpenSandbox Execd 执行和管理命令。"},
            {"name": "Terminals", "description": "通过 OpenSandbox Execd 访问交互式 PTY。"},
        ],
    )
    app.state.settings = resolved
    app.state.catalog = catalog
    app.state.journal = journal
    app.state.supervisor = supervisor
    app.state.execd = execd
    app.state.ready = False

    @app.middleware("http")
    async def restrict_bridge_peers(request: Request, call_next: Any) -> Any:
        client = request.client
        if client is None or not trusted_proxy_addresses.contains(client.host):
            return problem_response(
                ApiProblem(403, "bridge_peer_forbidden", "bridge peer is not allowed"), request
            )
        return await call_next(request)

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

    @app.get(
        "/healthz",
        tags=["Health"],
        summary="存活检查",
        description="无需认证；仅表示 bridge 进程存活。",
    )
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get(
        "/readyz",
        tags=["Health"],
        summary="就绪检查",
        description="无需认证；仅当 bridge 初始化完成时返回成功。",
    )
    async def ready(request: Request) -> dict[str, str]:
        if not request.app.state.ready:
            raise ApiProblem(503, "not_ready", "bridge is not ready")
        return {
            "status": "ready",
            "protocol_version": "1",
            "bridge_version": app.version,
        }

    def document_external_proxy_auth(credentials: ExternalBearerCredentials = None) -> None:
        """Expose proxy authentication in OpenAPI without handling its secret here."""
        del credentials

    router = APIRouter(prefix="/v1", dependencies=[Depends(document_external_proxy_auth)])
    session_router = APIRouter(tags=["Sessions"])
    runtime_router = APIRouter(tags=["Session runtime"])
    model_router = APIRouter(tags=["Models"])
    file_router = APIRouter(tags=["Files"])
    workspace_file_router = APIRouter(tags=["Workspace Files"])
    command_router = APIRouter(tags=["Commands"])
    terminal_router = APIRouter(tags=["Terminals"])
    terminal_websocket_router = APIRouter(prefix="/v1")
    register_session_routes(session_router, ctx)
    register_session_runtime_routes(runtime_router, ctx)
    register_model_routes(model_router, ctx)
    register_file_routes(file_router, ctx)
    register_workspace_file_routes(workspace_file_router, ctx)
    register_command_routes(command_router, ctx)
    register_terminal_routes(
        terminal_router,
        terminal_websocket_router,
        ctx,
        trusted_peer=trusted_proxy_addresses.contains,
    )
    router.include_router(session_router)
    router.include_router(runtime_router)
    router.include_router(model_router)
    router.include_router(file_router)
    router.include_router(workspace_file_router)
    router.include_router(command_router)
    router.include_router(terminal_router)
    app.include_router(router)
    app.include_router(terminal_websocket_router)
    return app
