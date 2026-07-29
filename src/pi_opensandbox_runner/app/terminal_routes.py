from __future__ import annotations

import asyncio
from collections.abc import Callable
from contextlib import suppress
from urllib.parse import urlencode

from fastapi import APIRouter, Query, Response, WebSocket
from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import ConnectionClosed, InvalidStatus

from ..schemas import TerminalCreate
from .context import BridgeContext

MAX_TERMINAL_FRAME_BYTES = 65_536


def register_terminal_routes(
    router: APIRouter,
    websocket_router: APIRouter,
    ctx: BridgeContext,
    *,
    trusted_peer: Callable[[str], bool],
) -> None:
    @router.post(
        "/terminals",
        status_code=201,
        summary="创建交互式 Terminal",
        description=(
            "在 OpenSandbox Execd 中创建 PTY shell。shell 在首次 WebSocket 连接时启动；"
            "cwd 只是初始目录，不是权限边界。"
        ),
    )
    async def create_terminal(payload: TerminalCreate) -> Response:
        return await ctx.execd_request("POST", "/pty", json_body=payload.model_dump())

    @router.get(
        "/terminals/{terminal_id}",
        summary="查询 Terminal 状态",
        description="返回 PTY 是否运行以及用于断线 replay 的 output_offset。",
    )
    async def terminal_status(terminal_id: str) -> Response:
        return await ctx.execd_request("GET", f"/pty/{terminal_id}")

    @router.delete(
        "/terminals/{terminal_id}",
        summary="删除交互式 Terminal",
        description="终止 PTY shell 并释放 Execd 中保存的 replay 状态。",
    )
    async def delete_terminal(terminal_id: str) -> Response:
        return await ctx.execd_request("DELETE", f"/pty/{terminal_id}")

    @websocket_router.websocket("/terminals/{terminal_id}/ws")
    async def terminal_socket(
        websocket: WebSocket,
        terminal_id: str,
        since: int = Query(default=0, ge=0),
        takeover: bool = Query(default=False),
    ) -> None:
        peer = websocket.client
        if peer is None or not trusted_peer(peer.host):
            await websocket.close(code=1008, reason="bridge peer is not allowed")
            return
        query = urlencode(
            {"since": since, **({"takeover": "1"} if takeover else {})}
        )
        upstream_url = ctx.execd.websocket_url(
            f"/pty/{terminal_id}/ws",
            query,
        )
        try:
            async with connect(
                upstream_url,
                compression=None,
                max_size=MAX_TERMINAL_FRAME_BYTES,
            ) as upstream:
                await websocket.accept()
                await _relay(websocket, upstream)
        except InvalidStatus:
            await websocket.close(code=1008, reason="terminal is unavailable or already connected")
        except OSError:
            await websocket.close(code=1011, reason="Execd terminal is unavailable")


async def _relay(websocket: WebSocket, upstream: ClientConnection) -> None:
    async def downstream_to_upstream() -> None:
        while True:
            message = await websocket.receive()
            message_type = message["type"]
            if message_type == "websocket.disconnect":
                await upstream.close(
                    code=int(message.get("code") or 1000),
                    reason=str(message.get("reason") or ""),
                )
                return
            data = message.get("bytes")
            if data is None:
                data = message.get("text")
            if data is not None:
                size = len(data) if isinstance(data, bytes) else len(data.encode())
                if size > MAX_TERMINAL_FRAME_BYTES:
                    await websocket.close(code=1009, reason="terminal frame is too large")
                    return
                await upstream.send(data)

    async def upstream_to_downstream() -> None:
        try:
            while True:
                data = await upstream.recv()
                if isinstance(data, bytes):
                    await websocket.send_bytes(data)
                else:
                    await websocket.send_text(data)
        except ConnectionClosed as exc:
            with suppress(RuntimeError):
                await websocket.close(code=exc.code or 1000, reason=exc.reason or "")

    first = asyncio.create_task(downstream_to_upstream())
    second = asyncio.create_task(upstream_to_downstream())
    done, pending = await asyncio.wait(
        {first, second},
        return_when=asyncio.FIRST_COMPLETED,
    )
    for task in pending:
        task.cancel()
    await asyncio.gather(*done, *pending, return_exceptions=True)
