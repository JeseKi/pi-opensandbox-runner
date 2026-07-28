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
from ..model_catalog import ModelCatalog
from ..rpc import SessionSupervisor
from .command_routes import register_command_routes
from .context import BridgeContext
from .file_routes import register_file_routes
from .mcp_routes import register_mcp_routes
from .model_routes import register_model_routes
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
    model_catalog = ModelCatalog(
        resolved.model_catalog_path,
        litellm_api_base=resolved.litellm_api_base,
        virtual_key=resolved.litellm_virtual_key,
    )
    ctx = BridgeContext(resolved, catalog, journal, supervisor, execd, model_catalog)

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
            "除健康检查外，所有 `/v1` 接口均需要 `Authorization: Bearer <bridge-token>`。"
            "`cwd` 是 Pi 初始工作目录，不是权限边界。"
        ),
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_tags=[
            {"name": "Health", "description": "Bridge 存活与就绪状态。"},
            {"name": "Sessions", "description": "Pi Session 的创建、配置、历史与 prompt。"},
            {"name": "Session runtime", "description": "运行中的 Pi 控制、上下文和 SSE 事件。"},
            {"name": "MCP", "description": "远程 MCP Server 注册表与 Session 绑定。"},
            {"name": "Models", "description": "Pi LiteLLM 模型目录管理。"},
            {"name": "Files", "description": "通过 OpenSandbox Execd 浏览和修改容器文件。"},
            {"name": "Commands", "description": "通过 OpenSandbox Execd 执行和管理命令。"},
        ],
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
        return {"status": "ready"}

    def authenticate(credentials: BearerCredentials = None) -> None:
        if (
            credentials is None
            or credentials.scheme.lower() != "bearer"
            or not hmac.compare_digest(credentials.credentials, resolved.api_token)
        ):
            raise ApiProblem(401, "unauthorized", "a valid bearer token is required")

    router = APIRouter(prefix="/v1", dependencies=[Depends(authenticate)])
    session_router = APIRouter(tags=["Sessions"])
    runtime_router = APIRouter(tags=["Session runtime"])
    mcp_router = APIRouter(tags=["MCP"])
    model_router = APIRouter(tags=["Models"])
    file_router = APIRouter(tags=["Files"])
    command_router = APIRouter(tags=["Commands"])
    register_session_routes(session_router, ctx)
    register_session_runtime_routes(runtime_router, ctx)
    register_mcp_routes(mcp_router, ctx)
    register_model_routes(model_router, ctx)
    register_file_routes(file_router, ctx)
    register_command_routes(command_router, ctx)
    router.include_router(session_router)
    router.include_router(runtime_router)
    router.include_router(mcp_router)
    router.include_router(model_router)
    router.include_router(file_router)
    router.include_router(command_router)
    app.include_router(router)
    return app
