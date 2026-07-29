from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

from fastapi import Depends, FastAPI, Query, Response
from sqlalchemy import and_, desc, func, or_, select

from ...clients import BridgeClient, UpstreamProblem, as_manager_problem
from ...config import ManagerSettings
from ...crypto import CredentialCipher
from ...database import ManagerDatabase
from ...models import RunnerInstance, SessionBinding, TerminalBinding
from ...openapi_docs import api_doc
from ...problems import ManagerProblem
from ...schemas import TerminalCreate, TerminalOut, TerminalPage
from ...security import Principal
from ..helpers import _connection, _owned_instance
from ..pagination import _decode_page_cursor, _encode_page_cursor
from .helpers import _load_owned_terminal, _terminal_out
from .lifecycle import _mark_terminal_closed


def register_terminal_crud_routes(
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
