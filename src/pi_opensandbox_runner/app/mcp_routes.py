from __future__ import annotations

from typing import cast
from urllib.parse import urlparse
from uuid import uuid4

from fastapi import APIRouter, Response
from sqlalchemy.exc import IntegrityError

from ..catalog import McpServerRecord
from ..schemas import (
    McpServerCreate,
    McpServerOut,
    McpServerPatch,
    McpTransport,
    SessionMcpServersOut,
    SessionMcpServersUpdate,
)
from .context import BridgeContext
from .problems import ApiProblem


def mcp_server_out(record: McpServerRecord) -> McpServerOut:
    return McpServerOut(
        id=record.id,
        name=record.name,
        transport=cast(McpTransport, record.transport),
        url=record.url,
        headers=record.headers_template,
        request_timeout_ms=record.request_timeout_ms,
        created_at=record.created_at,
        updated_at=record.updated_at,
    )


def validate_mcp_url(url: str, *, allow_insecure_http: bool) -> None:
    parsed = urlparse(url)
    if not parsed.netloc or parsed.scheme not in {"https", "http"}:
        raise ApiProblem(422, "invalid_mcp_url", "MCP URL must be an absolute HTTP(S) URL")
    if parsed.scheme == "http" and not allow_insecure_http:
        raise ApiProblem(
            422,
            "insecure_mcp_url",
            "HTTP MCP URLs require MCP_ALLOW_INSECURE_HTTP=1",
        )


def register_mcp_routes(router: APIRouter, ctx: BridgeContext) -> None:
    @router.post(
        "/mcp/servers",
        response_model=McpServerOut,
        status_code=201,
        summary="创建 MCP Server",
        description=(
            "创建可被多个 Session 绑定的远程 MCP Server。首期只支持 Streamable HTTP 和 SSE；"
            "Header 仅保存 ${MCP_*} 模板，实际 token 通过容器环境变量提供。"
        ),
    )
    async def create_mcp_server(payload: McpServerCreate) -> McpServerOut:
        validate_mcp_url(payload.url, allow_insecure_http=ctx.settings.mcp_allow_insecure_http)
        try:
            record = await ctx.catalog.create_mcp_server(
                server_id=str(uuid4()),
                name=payload.name,
                transport=payload.transport,
                url=payload.url,
                headers_template=payload.headers,
                request_timeout_ms=payload.request_timeout_ms,
            )
        except IntegrityError as exc:
            raise ApiProblem(
                409, "mcp_server_name_conflict", "MCP Server name already exists"
            ) from exc
        return mcp_server_out(record)

    @router.get(
        "/mcp/servers",
        response_model=list[McpServerOut],
        summary="列出 MCP Server",
        description="返回容器内所有 MCP Server 配置；Header 值始终是未展开的环境变量模板。",
    )
    async def list_mcp_servers() -> list[McpServerOut]:
        return [mcp_server_out(item) for item in await ctx.catalog.list_mcp_servers()]

    @router.get(
        "/mcp/servers/{server_id}",
        response_model=McpServerOut,
        summary="读取 MCP Server",
    )
    async def get_mcp_server(server_id: str) -> McpServerOut:
        record = await ctx.catalog.get_mcp_server(server_id)
        if record is None:
            raise ApiProblem(404, "mcp_server_not_found", "MCP Server does not exist")
        return mcp_server_out(record)

    @router.patch(
        "/mcp/servers/{server_id}",
        response_model=McpServerOut,
        summary="更新 MCP Server",
        description=(
            "更新会在已绑定 Session 的下一条非流式 prompt 前生效；不会中断正在运行的 Pi。"
        ),
    )
    async def patch_mcp_server(server_id: str, payload: McpServerPatch) -> McpServerOut:
        values = payload.model_dump(exclude_none=True)
        if not values:
            raise ApiProblem(422, "mcp_update_empty", "at least one MCP Server field is required")
        if url := values.get("url"):
            validate_mcp_url(url, allow_insecure_http=ctx.settings.mcp_allow_insecure_http)
        try:
            record = await ctx.catalog.update_mcp_server(server_id, **values)
        except IntegrityError as exc:
            raise ApiProblem(
                409, "mcp_server_name_conflict", "MCP Server name already exists"
            ) from exc
        if record is None:
            raise ApiProblem(404, "mcp_server_not_found", "MCP Server does not exist")
        return mcp_server_out(record)

    @router.delete(
        "/mcp/servers/{server_id}",
        status_code=204,
        summary="删除 MCP Server",
        description="仍被任一 Session 绑定时返回 409 mcp_server_in_use；请先解除绑定。",
    )
    async def delete_mcp_server(server_id: str) -> Response:
        result = await ctx.catalog.delete_mcp_server(server_id)
        if result is None:
            raise ApiProblem(404, "mcp_server_not_found", "MCP Server does not exist")
        if not result:
            raise ApiProblem(409, "mcp_server_in_use", "unbind this MCP Server before deleting it")
        return Response(status_code=204)

    @router.get(
        "/sessions/{session_id}/mcp-servers",
        response_model=SessionMcpServersOut,
        summary="读取 Session MCP 绑定",
        description="仅返回保存的配置，不代表远程 MCP 当前连接健康。",
    )
    async def get_session_mcp_servers(session_id: str) -> SessionMcpServersOut:
        items = await ctx.catalog.get_session_mcp_servers(session_id)
        if items is None:
            raise ApiProblem(404, "session_not_found", "session does not exist")
        return SessionMcpServersOut(
            session_id=session_id, items=[mcp_server_out(item) for item in items]
        )

    @router.put(
        "/sessions/{session_id}/mcp-servers",
        response_model=SessionMcpServersOut,
        summary="替换 Session MCP 绑定",
        description=(
            "完整替换绑定集合。正在生成时返回 409 session_streaming；idle Pi 会在下一条 prompt 前"
            "以新工具配置重启。"
        ),
    )
    async def put_session_mcp_servers(
        session_id: str, payload: SessionMcpServersUpdate
    ) -> SessionMcpServersOut:
        async with ctx.supervisor.command_lock(session_id):
            await ctx.require_session(session_id)
            process = ctx.supervisor.active(session_id)
            if process is not None and process.is_streaming:
                raise ApiProblem(
                    409,
                    "session_streaming",
                    "stop or wait for the current agent turn before changing MCP Servers",
                )
            try:
                items = await ctx.catalog.set_session_mcp_servers(session_id, payload.server_ids)
            except KeyError as exc:
                raise ApiProblem(
                    422, "mcp_server_not_found", "one or more MCP Servers do not exist"
                ) from exc
        assert items is not None
        return SessionMcpServersOut(
            session_id=session_id, items=[mcp_server_out(item) for item in items]
        )
