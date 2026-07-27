from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import APIRouter, Query, Response
from fastapi.responses import StreamingResponse

from ..execd import ExecdError
from ..schemas import CommandCreate
from .context import BridgeContext
from .problems import ApiProblem


def register_command_routes(router: APIRouter, ctx: BridgeContext) -> None:
    @router.post(
        "/commands",
        summary="在容器中执行命令",
        description=(
            "通过 OpenSandbox Execd 执行，不经过 Pi 对话上下文。"
            "默认前台执行并流式返回输出；background=true 时用状态与日志接口轮询。"
            "命令拥有 bridge 容器进程的文件访问权限，不受 workspace 限制。"
        ),
    )
    async def run_command(payload: CommandCreate) -> Response:
        try:
            upstream = await ctx.execd.stream(
                "POST", "/command", json=payload.model_dump(exclude_none=True)
            )
        except ExecdError as exc:
            raise ApiProblem(exc.status_code, "execd_request_failed", exc.detail) from exc

        async def body() -> AsyncIterator[bytes]:
            try:
                async for chunk in upstream.aiter_bytes():
                    yield chunk
            finally:
                await upstream.aclose()

        return StreamingResponse(
            body(),
            status_code=upstream.status_code,
            media_type=upstream.headers.get("content-type", "text/event-stream"),
        )

    @router.delete(
        "/commands/{command_id}",
        status_code=202,
        summary="中止后台命令",
        description="命令 id 来自 OpenSandbox 命令执行响应。",
    )
    async def abort_command(command_id: str) -> Response:
        return await ctx.execd_request("DELETE", "/command", params={"id": command_id})

    @router.get("/commands/{command_id}", summary="查询命令状态")
    async def command_status(command_id: str) -> Response:
        return await ctx.execd_request("GET", f"/command/status/{command_id}")

    @router.get(
        "/commands/{command_id}/logs",
        summary="读取后台命令日志",
        description="cursor 是 OpenSandbox 日志游标；响应头可提供下一游标。",
    )
    async def command_logs(
        command_id: str,
        cursor: Annotated[
            int | None, Query(ge=0, description="从该 OpenSandbox 日志游标之后读取。")
        ] = None,
    ) -> Response:
        params = {"cursor": cursor} if cursor is not None else None
        return await ctx.execd_request("GET", f"/command/{command_id}/logs", params=params)
