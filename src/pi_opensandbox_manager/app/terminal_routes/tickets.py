from __future__ import annotations

import secrets
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

from fastapi import Depends, FastAPI
from sqlalchemy import delete, or_

from ...config import ManagerSettings
from ...crypto import CredentialCipher
from ...database import ManagerDatabase
from ...models import RunnerInstance, TerminalBinding, TerminalTicket
from ...openapi_docs import api_doc
from ...problems import ManagerProblem
from ...schemas import TerminalTicketCreate, TerminalTicketOut
from ...security import Principal, token_hash
from ..helpers import _connection
from .constants import TERMINAL_PROTOCOL, TERMINAL_TICKET_PREFIX
from .helpers import _as_utc, _load_owned_terminal


def register_terminal_ticket_route(
    app: FastAPI,
    database: ManagerDatabase,
    settings: ManagerSettings,
    cipher: CredentialCipher,
    service_dependency: Any,
) -> None:
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
        if terminal.state in {"closed", "unavailable"} or _as_utc(terminal.expires_at) <= now:
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
            subprotocols=[TERMINAL_PROTOCOL, f"{TERMINAL_TICKET_PREFIX}{secret}"],
            expires_at=expires_at,
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
        if instance is None or instance.state != "ready" or _as_utc(terminal.expires_at) <= now:
            return None
        terminal.state = "connected"
        terminal.connected_at = now
        terminal.disconnected_at = None
        db.flush()
        return terminal, _connection(instance, cipher)
