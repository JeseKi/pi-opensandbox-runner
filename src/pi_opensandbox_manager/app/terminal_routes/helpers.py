from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select

from ...crypto import CredentialCipher
from ...database import ManagerDatabase
from ...models import RunnerInstance, TerminalBinding
from ...problems import ManagerProblem
from ...schemas import TerminalOut
from ...security import Principal
from ..helpers import _connection, _owned_instance
from .constants import TERMINAL_WARNINGS


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
    terminal: TerminalBinding,
    instance: RunnerInstance,
) -> TerminalOut:
    return TerminalOut(
        id=terminal.id,
        subject_ref=instance.subject_ref,
        session_id=terminal.external_session_id,
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


def _as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
