from __future__ import annotations

from typing import Any

from fastapi import FastAPI

from ...config import ManagerSettings
from ...crypto import CredentialCipher
from ...database import ManagerDatabase
from .connection import register_terminal_connection_route
from .constants import (
    MAX_TERMINAL_FRAME_BYTES,
    TERMINAL_PROTOCOL,
    TERMINAL_TICKET_PREFIX,
    TERMINAL_WARNINGS,
)
from .crud import register_terminal_crud_routes
from .lifecycle import cleanup_terminals, mark_connected_terminals_detached
from .tickets import _consume_ticket, register_terminal_ticket_route

__all__ = [
    "MAX_TERMINAL_FRAME_BYTES",
    "TERMINAL_PROTOCOL",
    "TERMINAL_TICKET_PREFIX",
    "TERMINAL_WARNINGS",
    "_consume_ticket",
    "cleanup_terminals",
    "mark_connected_terminals_detached",
    "register_terminal_routes",
]


def register_terminal_routes(
    app: FastAPI,
    database: ManagerDatabase,
    settings: ManagerSettings,
    cipher: CredentialCipher,
    service_dependency: Any,
) -> None:
    register_terminal_crud_routes(app, database, settings, cipher, service_dependency)
    register_terminal_ticket_route(app, database, settings, cipher, service_dependency)
    register_terminal_connection_route(app, database, cipher)
