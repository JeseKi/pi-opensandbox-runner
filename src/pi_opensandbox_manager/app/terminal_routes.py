from __future__ import annotations

import asyncio
import json
import secrets
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlencode, urlsplit, urlunsplit
from uuid import uuid4

from fastapi import Depends, FastAPI, Query, Response, WebSocket
from sqlalchemy import and_, delete, desc, func, or_, select
from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import ConnectionClosed, InvalidStatus

from ..clients import BridgeClient, UpstreamProblem, as_manager_problem
from ..config import ManagerSettings
from ..crypto import CredentialCipher
from ..database import ManagerDatabase
from ..models import RunnerInstance, SessionBinding, TerminalBinding, TerminalTicket
from ..openapi_docs import api_doc
from ..problems import ManagerProblem
from ..schemas import (
    TerminalCreate,
    TerminalOut,
    TerminalPage,
    TerminalTicketCreate,
    TerminalTicketOut,
)
from ..security import Principal, token_hash
from .helpers import _connection, _owned_instance
from .pagination import _decode_page_cursor, _encode_page_cursor

TERMINAL_PROTOCOL = "pi-terminal.v1"
TERMINAL_TICKET_PREFIX = "pi-terminal-ticket."
MAX_TERMINAL_FRAME_BYTES = 65_536
TERMINAL_WARNINGS = [
    "Terminal 拥有整个 sandbox 的文件权限，不受 cwd 限制。",
    "Agent Turn 与 Terminal 可以并发修改 workspace，调用方必须处理冲突。",
]


def register_terminal_routes(
    app: FastAPI,
    database: ManagerDatabase,
    settings: ManagerSettings,
    cipher: CredentialCipher,
    service_dependency: Any,
) -> None:
    @app.post(
        "/v1/instances/{subject_ref}/terminals",
        response_model=TerminalOut,
        status_code=201,
        **api_doc(
            summary="创建交互式 Terminal",
            description=(
                "为 ready Instance 创建独立 Bash PTY。可选 session_id 只决定初始 cwd；Terminal "
                "仍可访问整个 sandbox。Terminal 与 Agent Turn 允许并发运行。需要 "
                "`terminals:access` scope。"
            ),
            tag="Terminal（交互终端）",
            operation_id="create_manager_terminal",
            response_description="新建 Terminal 的状态。",
        ),
    )
    async def create_terminal(
        payload: TerminalCreate,
        subject_ref: str,
        caller: Principal = Depends(service_dependency),
    ) -> TerminalOut:
        caller.require("terminals:access")
        with database.session() as db:
            instance = _owned_instance(db, caller, subject_ref)
            bridge = _connection(instance, cipher)
            active_count = db.scalar(
                select(func.count(TerminalBinding.id)).where(
                    TerminalBinding.instance_id == instance.id,
                    TerminalBinding.state != "closed",
                )
            )
            if int(active_count or 0) >= settings.max_terminals_per_instance:
                raise ManagerProblem(
                    409,
                    "terminal_limit_reached",
                    "the Instance has reached its active Terminal limit",
                )
            session_binding: SessionBinding | None = None
            if payload.session_id is not None:
                session_binding = db.scalar(
                    select(SessionBinding).where(
                        SessionBinding.instance_id == instance.id,
                        SessionBinding.external_session_id == payload.session_id,
                    )
                )
                if session_binding is None:
                    raise ManagerProblem(404, "session_not_found", "session not found")
            cwd = session_binding.cwd if session_binding else "/root/workspace"
            instance_id = instance.id
            session_binding_id = session_binding.id if session_binding else None

        client = BridgeClient(settings, bridge.bridge_url, bridge.bridge_token)
        try:
            upstream = await asyncio.to_thread(client.create_terminal, cwd)
        except UpstreamProblem as exc:
            raise as_manager_problem(exc) from exc
        finally:
            client.close()

        now = datetime.now(UTC)
        terminal = TerminalBinding(
            id=str(uuid4()),
            instance_id=instance_id,
            session_binding_id=session_binding_id,
            upstream_terminal_id=str(upstream["session_id"]),
            cwd=cwd,
            state="created",
            expires_at=now + timedelta(seconds=settings.terminal_max_lifetime_seconds),
        )
        with database.session() as db:
            db.add(terminal)
            db.flush()
            current_instance = db.get(RunnerInstance, instance_id)
            assert current_instance is not None
            return _terminal_out(db, terminal, current_instance)

    @app.get(
        "/v1/instances/{subject_ref}/terminals",
        response_model=TerminalPage,
        **api_doc(
            summary="分页列出 Terminal",
            description=(
                "列出当前 consumer 的 Instance Terminal，固定按创建时间倒序排列。"
                "需要 `terminals:access` scope。"
            ),
            tag="Terminal（交互终端）",
            operation_id="list_manager_terminals",
            response_description="一页 Terminal 和下一页游标。",
        ),
    )
    async def list_terminals(
        subject_ref: str,
        cursor: str | None = Query(default=None, min_length=1, max_length=1024),
        limit: int = Query(default=100, ge=1, le=500),
        caller: Principal = Depends(service_dependency),
    ) -> TerminalPage:
        caller.require("terminals:access")
        position = _decode_page_cursor(cursor, "terminal") if cursor else None
        with database.session() as db:
            instance = _owned_instance(db, caller, subject_ref)
            statement = select(TerminalBinding).where(
                TerminalBinding.instance_id == instance.id
            )
            if position is not None:
                statement = statement.where(
                    or_(
                        TerminalBinding.created_at < position.created_at,
                        and_(
                            TerminalBinding.created_at == position.created_at,
                            TerminalBinding.id < position.item_id,
                        ),
                    )
                )
            records = list(
                db.scalars(
                    statement.order_by(
                        desc(TerminalBinding.created_at),
                        desc(TerminalBinding.id),
                    ).limit(limit + 1)
                )
            )
            has_more = len(records) > limit
            page = records[:limit]
            return TerminalPage(
                items=[_terminal_out(db, item, instance) for item in page],
                next_cursor=(
                    _encode_page_cursor(page[-1].created_at, page[-1].id)
                    if has_more and page
                    else None
                ),
                has_more=has_more,
            )

    @app.get(
        "/v1/instances/{subject_ref}/terminals/{terminal_id}",
        response_model=TerminalOut,
        **api_doc(
            summary="查询 Terminal 状态",
            description=(
                "刷新 PTY running 状态和 output_offset。output_offset 可用于断线后的 since replay。"
                "需要 `terminals:access` scope。"
            ),
            tag="Terminal（交互终端）",
            operation_id="get_manager_terminal",
            response_description="Terminal 当前状态。",
        ),
    )
    async def get_terminal(
        subject_ref: str,
        terminal_id: str,
        caller: Principal = Depends(service_dependency),
    ) -> TerminalOut:
        caller.require("terminals:access")
        terminal, instance, bridge = _load_owned_terminal(
            database, cipher, caller, subject_ref, terminal_id
        )
        status: dict[str, Any] | None = None
        if terminal.state not in {"closed", "unavailable"}:
            client = BridgeClient(settings, bridge.bridge_url, bridge.bridge_token)
            try:
                status = await asyncio.to_thread(
                    client.terminal_status, terminal.upstream_terminal_id
                )
            except UpstreamProblem as exc:
                if exc.status_code == 404:
                    terminal.state = "unavailable"
                else:
                    raise as_manager_problem(exc) from exc
            finally:
                client.close()
            if status is not None:
                terminal.output_offset = int(status.get("output_offset") or 0)
                if status.get("running") is False and terminal.state == "connected":
                    terminal.state = "exited"
        with database.session() as db:
            stored = db.get(TerminalBinding, terminal.id)
            assert stored is not None
            stored.state = terminal.state
            stored.output_offset = terminal.output_offset
            db.flush()
            current_instance = db.get(RunnerInstance, instance.id)
            assert current_instance is not None
            return _terminal_out(db, stored, current_instance)

    @app.delete(
        "/v1/instances/{subject_ref}/terminals/{terminal_id}",
        status_code=204,
        response_model=None,
        **api_doc(
            summary="删除 Terminal",
            description=(
                "终止 PTY、撤销尚未使用的 ticket，并将 Terminal 标记为 closed。"
                "重复删除 closed Terminal 仍成功。需要 `terminals:access` scope。"
            ),
            tag="Terminal（交互终端）",
            operation_id="delete_manager_terminal",
            response_description="Terminal 已关闭。",
        ),
    )
    async def delete_terminal(
        subject_ref: str,
        terminal_id: str,
        caller: Principal = Depends(service_dependency),
    ) -> Response:
        caller.require("terminals:access")
        terminal, _, bridge = _load_owned_terminal(
            database, cipher, caller, subject_ref, terminal_id
        )
        if terminal.state != "closed":
            client = BridgeClient(settings, bridge.bridge_url, bridge.bridge_token)
            try:
                await asyncio.to_thread(client.delete_terminal, terminal.upstream_terminal_id)
            except UpstreamProblem as exc:
                if exc.status_code != 404:
                    raise as_manager_problem(exc) from exc
            finally:
                client.close()
        _mark_terminal_closed(database, terminal.id)
        return Response(status_code=204)

    @app.post(
        "/v1/instances/{subject_ref}/terminals/{terminal_id}/tickets",
        response_model=TerminalTicketOut,
        status_code=201,
        **api_doc(
            summary="签发一次性 Terminal 连接票据",
            description=(
                "外部后端在完成最终用户授权后调用。ticket 绑定精确 Origin、60 秒有效且只能"
                "使用一次；Manager service token 不得交给浏览器。需要 `terminals:access` scope。"
            ),
            tag="Terminal（交互终端）",
            operation_id="create_manager_terminal_ticket",
            response_description="浏览器 WebSocket 连接描述。",
        ),
    )
    async def create_ticket(
        payload: TerminalTicketCreate,
        subject_ref: str,
        terminal_id: str,
        caller: Principal = Depends(service_dependency),
    ) -> TerminalTicketOut:
        caller.require("terminals:access")
        if payload.origin not in settings.allowed_terminal_origins:
            raise ManagerProblem(
                422,
                "terminal_origin_not_allowed",
                "origin is not in RUNNER_MANAGER_TERMINAL_ALLOWED_ORIGINS",
            )
        terminal, _, _ = _load_owned_terminal(
            database, cipher, caller, subject_ref, terminal_id
        )
        now = datetime.now(UTC)
        if (
            terminal.state in {"closed", "unavailable"}
            or _as_utc(terminal.expires_at) <= now
        ):
            raise ManagerProblem(409, "terminal_unavailable", "terminal is not connectable")
        secret = f"rm_pty_{secrets.token_urlsafe(32)}"
        expires_at = now + timedelta(seconds=settings.terminal_ticket_ttl_seconds)
        with database.session() as db:
            db.execute(
                delete(TerminalTicket).where(
                    or_(
                        TerminalTicket.terminal_id == terminal.id,
                        TerminalTicket.expires_at <= now,
                    )
                )
            )
            db.add(
                TerminalTicket(
                    id=str(uuid4()),
                    terminal_id=terminal.id,
                    token_hash=token_hash(secret),
                    origin=payload.origin,
                    expires_at=expires_at,
                )
            )
        return TerminalTicketOut(
            websocket_url=settings.terminal_public_ws_url,
            subprotocols=[
                TERMINAL_PROTOCOL,
                f"{TERMINAL_TICKET_PREFIX}{secret}",
            ],
            expires_at=expires_at,
        )

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
        query = urlencode(
            {"since": since, **({"takeover": "1"} if takeover else {})}
        )
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


def _load_owned_terminal(
    database: ManagerDatabase,
    cipher: CredentialCipher,
    caller: Principal,
    subject_ref: str,
    terminal_id: str,
) -> tuple[TerminalBinding, RunnerInstance, Any]:
    with database.session() as db:
        instance = _owned_instance(db, caller, subject_ref)
        terminal = db.scalar(
            select(TerminalBinding).where(
                TerminalBinding.id == terminal_id,
                TerminalBinding.instance_id == instance.id,
            )
        )
        if terminal is None:
            raise ManagerProblem(404, "terminal_not_found", "terminal not found")
        return terminal, instance, _connection(instance, cipher)


def _terminal_out(
    db: Any,
    terminal: TerminalBinding,
    instance: RunnerInstance,
) -> TerminalOut:
    session_id = None
    if terminal.session_binding_id:
        session = db.get(SessionBinding, terminal.session_binding_id)
        session_id = session.external_session_id if session else None
    return TerminalOut(
        id=terminal.id,
        subject_ref=instance.subject_ref,
        session_id=session_id,
        cwd=terminal.cwd,
        state=terminal.state,
        output_offset=terminal.output_offset,
        warnings=TERMINAL_WARNINGS,
        created_at=terminal.created_at,
        updated_at=terminal.updated_at,
        connected_at=terminal.connected_at,
        disconnected_at=terminal.disconnected_at,
        expires_at=terminal.expires_at,
    )


def _ticket_from_subprotocols(protocols: Any) -> str | None:
    if TERMINAL_PROTOCOL not in protocols:
        return None
    for value in protocols:
        if value.startswith(TERMINAL_TICKET_PREFIX):
            ticket = value.removeprefix(TERMINAL_TICKET_PREFIX)
            return ticket or None
    return None


def _consume_ticket(
    database: ManagerDatabase,
    cipher: CredentialCipher,
    ticket: str | None,
    origin: str,
) -> tuple[TerminalBinding, Any] | None:
    if not ticket or not origin:
        return None
    now = datetime.now(UTC)
    with database.session() as db:
        terminal_id = db.execute(
            delete(TerminalTicket)
            .where(
                TerminalTicket.token_hash == token_hash(ticket),
                TerminalTicket.origin == origin,
                TerminalTicket.expires_at > now,
            )
            .returning(TerminalTicket.terminal_id)
        ).scalar_one_or_none()
        if terminal_id is None:
            return None
        terminal = db.get(TerminalBinding, terminal_id)
        if terminal is None or terminal.state in {"closed", "unavailable"}:
            return None
        instance = db.get(RunnerInstance, terminal.instance_id)
        if (
            instance is None
            or instance.state != "ready"
            or _as_utc(terminal.expires_at) <= now
        ):
            return None
        terminal.state = "connected"
        terminal.connected_at = now
        terminal.disconnected_at = None
        db.flush()
        return terminal, _connection(instance, cipher)


def _bridge_terminal_ws_url(
    bridge_url: str,
    upstream_terminal_id: str,
    query: str,
) -> str:
    parsed = urlsplit(bridge_url)
    scheme = "wss" if parsed.scheme == "https" else "ws"
    path = (
        f"{parsed.path.rstrip('/')}/v1/terminals/{upstream_terminal_id}/ws"
    )
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


def _as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _finish_terminal_connection(
    database: ManagerDatabase,
    terminal_id: str,
    result: dict[str, Any],
) -> None:
    now = datetime.now(UTC)
    with database.session() as db:
        terminal = db.get(TerminalBinding, terminal_id)
        if terminal is None:
            return
        terminal.output_offset = int(result["output_offset"])
        terminal.state = str(result["state"])
        terminal.disconnected_at = now


def _mark_terminal_detached(
    database: ManagerDatabase,
    terminal_id: str,
    output_offset: int,
) -> None:
    _finish_terminal_connection(
        database,
        terminal_id,
        {"state": "detached", "output_offset": output_offset},
    )


def _mark_terminal_closed(database: ManagerDatabase, terminal_id: str) -> None:
    with database.session() as db:
        terminal = db.get(TerminalBinding, terminal_id)
        if terminal is None:
            return
        terminal.state = "closed"
        terminal.disconnected_at = datetime.now(UTC)
        db.execute(delete(TerminalTicket).where(TerminalTicket.terminal_id == terminal_id))


async def cleanup_terminals(
    database: ManagerDatabase,
    settings: ManagerSettings,
    cipher: CredentialCipher,
) -> None:
    now = datetime.now(UTC)
    idle_before = now - timedelta(seconds=settings.terminal_detached_ttl_seconds)
    with database.session() as db:
        db.execute(delete(TerminalTicket).where(TerminalTicket.expires_at <= now))
        records = list(
            db.scalars(
                select(TerminalBinding).where(
                    TerminalBinding.state != "closed",
                    or_(
                        TerminalBinding.expires_at <= now,
                        and_(
                            TerminalBinding.state.in_(
                                ["created", "detached", "exited", "unavailable"]
                            ),
                            func.coalesce(
                                TerminalBinding.disconnected_at,
                                TerminalBinding.created_at,
                            )
                            <= idle_before,
                        ),
                        TerminalBinding.instance_id.in_(
                            select(RunnerInstance.id).where(
                                RunnerInstance.state != "ready"
                            )
                        ),
                    ),
                )
            )
        )
        cleanup_inputs: list[tuple[str, str, Any | None]] = []
        for terminal in records:
            instance = db.get(RunnerInstance, terminal.instance_id)
            bridge = None
            if instance is not None and instance.state == "ready":
                with suppress(ManagerProblem):
                    bridge = _connection(instance, cipher)
            cleanup_inputs.append(
                (terminal.id, terminal.upstream_terminal_id, bridge)
            )

    for terminal_id, upstream_id, bridge in cleanup_inputs:
        if bridge is not None:
            client = BridgeClient(settings, bridge.bridge_url, bridge.bridge_token)
            try:
                await asyncio.to_thread(client.delete_terminal, upstream_id)
            except UpstreamProblem:
                pass
            finally:
                client.close()
        _mark_terminal_closed(database, terminal_id)


def mark_connected_terminals_detached(database: ManagerDatabase) -> None:
    now = datetime.now(UTC)
    with database.session() as db:
        for terminal in db.scalars(
            select(TerminalBinding).where(TerminalBinding.state == "connected")
        ):
            terminal.state = "detached"
            terminal.disconnected_at = now
