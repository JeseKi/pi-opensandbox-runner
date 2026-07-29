from __future__ import annotations

import asyncio
import json
from contextlib import suppress
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlencode, urlsplit, urlunsplit

from fastapi import FastAPI, Query, WebSocket
from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import ConnectionClosed, InvalidStatus

from ...crypto import CredentialCipher
from ...database import ManagerDatabase
from .constants import MAX_TERMINAL_FRAME_BYTES, TERMINAL_PROTOCOL
from .helpers import _as_utc
from .lifecycle import (
    _finish_terminal_connection,
    _mark_terminal_detached,
)
from .tickets import _consume_ticket, _ticket_from_subprotocols


def register_terminal_connection_route(
    app: FastAPI,
    database: ManagerDatabase,
    cipher: CredentialCipher,
) -> None:
    @app.websocket("/v1/terminal-connections")
    async def terminal_connection(
        websocket: WebSocket,
        since: int = Query(default=0, ge=0),
        takeover: bool = Query(default=False),
    ) -> None:
        ticket = _ticket_from_subprotocols(websocket.scope.get("subprotocols", []))
        origin = websocket.headers.get("origin", "").rstrip("/")
        loaded = _consume_ticket(database, cipher, ticket, origin)
        if loaded is None:
            await websocket.close(code=1008, reason="invalid terminal connection ticket")
            return
        terminal, bridge = loaded
        query = urlencode({"since": since, **({"takeover": "1"} if takeover else {})})
        upstream_url = _bridge_terminal_ws_url(
            bridge.bridge_url,
            terminal.upstream_terminal_id,
            query,
        )
        try:
            async with connect(
                upstream_url,
                additional_headers={"Authorization": f"Bearer {bridge.bridge_token}"},
                compression=None,
                max_size=MAX_TERMINAL_FRAME_BYTES,
            ) as upstream:
                await websocket.accept(subprotocol=TERMINAL_PROTOCOL)
                result = await _relay_terminal(
                    websocket,
                    upstream,
                    terminal.output_offset,
                    _as_utc(terminal.expires_at),
                )
        except InvalidStatus:
            await websocket.close(code=1008, reason="terminal is unavailable or already connected")
            _mark_terminal_detached(database, terminal.id, terminal.output_offset)
            return
        except OSError:
            await websocket.close(code=1011, reason="terminal upstream is unavailable")
            _mark_terminal_detached(database, terminal.id, terminal.output_offset)
            return
        _finish_terminal_connection(database, terminal.id, result)


def _bridge_terminal_ws_url(
    bridge_url: str,
    upstream_terminal_id: str,
    query: str,
) -> str:
    parsed = urlsplit(bridge_url)
    scheme = "wss" if parsed.scheme == "https" else "ws"
    path = f"{parsed.path.rstrip('/')}/v1/terminals/{upstream_terminal_id}/ws"
    return urlunsplit((scheme, parsed.netloc, path, query, ""))


async def _relay_terminal(
    websocket: WebSocket,
    upstream: ClientConnection,
    output_offset: int,
    expires_at: datetime,
) -> dict[str, Any]:
    result: dict[str, Any] = {"state": "detached", "output_offset": output_offset}

    async def downstream_to_upstream() -> None:
        while True:
            message = await websocket.receive()
            if message["type"] == "websocket.disconnect":
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
                    _advance_output_offset(result, data)
                    await websocket.send_bytes(data)
                else:
                    try:
                        frame = json.loads(data)
                    except json.JSONDecodeError:
                        frame = {}
                    if isinstance(frame, dict) and frame.get("type") == "exit":
                        result["state"] = "exited"
                    await websocket.send_text(data)
        except ConnectionClosed as exc:
            with suppress(RuntimeError):
                await websocket.close(code=exc.code or 1000, reason=exc.reason or "")

    async def absolute_timeout() -> None:
        remaining = max(0.0, (expires_at - datetime.now(UTC)).total_seconds())
        await asyncio.sleep(remaining)
        result["state"] = "closed"
        await upstream.close(code=1008, reason="terminal lifetime expired")
        with suppress(RuntimeError):
            await websocket.close(code=1008, reason="terminal lifetime expired")

    tasks = {
        asyncio.create_task(downstream_to_upstream()),
        asyncio.create_task(upstream_to_downstream()),
        asyncio.create_task(absolute_timeout()),
    }
    done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    for task in pending:
        task.cancel()
    await asyncio.gather(*done, *pending, return_exceptions=True)
    return result


def _advance_output_offset(result: dict[str, Any], data: bytes) -> None:
    if not data:
        return
    current = int(result["output_offset"])
    if data[0] == 0x01:
        result["output_offset"] = current + len(data) - 1
    elif data[0] == 0x03 and len(data) >= 9:
        replay_start = int.from_bytes(data[1:9], "big")
        result["output_offset"] = max(current, replay_start + len(data) - 9)
