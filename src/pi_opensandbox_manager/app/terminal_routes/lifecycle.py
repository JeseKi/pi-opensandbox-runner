from __future__ import annotations

import asyncio
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import and_, delete, func, or_, select

from ...clients import BridgeClient, UpstreamProblem
from ...config import ManagerSettings
from ...crypto import CredentialCipher
from ...database import ManagerDatabase
from ...models import RunnerInstance, TerminalBinding, TerminalTicket
from ...problems import ManagerProblem
from ..helpers import _connection


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
                            select(RunnerInstance.id).where(RunnerInstance.state != "ready")
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
            cleanup_inputs.append((terminal.id, terminal.upstream_terminal_id, bridge))

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
