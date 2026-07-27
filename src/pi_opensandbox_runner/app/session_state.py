from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import Any, cast

from ..catalog import SessionRecord
from ..rpc import PiRpcProcess, RpcError, SessionSupervisor
from ..schemas import SessionOut, SystemPromptMode
from .problems import ApiProblem


async def get_entries(
    record: SessionRecord,
    supervisor: SessionSupervisor,
    cursor: str | None,
) -> tuple[list[dict[str, Any]], str | None]:
    process = supervisor.active(record.id)
    if process is not None:
        command: dict[str, Any] = {"type": "get_entries"}
        if cursor is not None:
            command["since"] = cursor
        response = await process.request(command)
        data = response.get("data")
        if not isinstance(data, dict):
            raise RpcError("Pi get_entries returned invalid data")
        entries = data.get("entries")
        return (
            [item for item in entries if isinstance(item, dict)]
            if isinstance(entries, list)
            else [],
            str(data["leafId"]) if data.get("leafId") is not None else None,
        )
    if not record.session_file or not Path(record.session_file).is_file():
        if cursor is not None:
            raise ApiProblem(422, "invalid_cursor", "entry cursor does not exist")
        return [], None
    stored = read_session_entries(Path(record.session_file))
    leaf_id = str(stored[-1].get("id")) if stored else None
    if cursor is None:
        return stored, leaf_id
    for index, entry in enumerate(stored):
        if entry.get("id") == cursor:
            return stored[index + 1 :], leaf_id
    raise ApiProblem(422, "invalid_cursor", "entry cursor does not exist")


def read_session_entries(path: Path) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    try:
        with path.open(encoding="utf-8") as stream:
            for line in stream:
                try:
                    item = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(item, dict) and item.get("type") != "session":
                    entries.append(item)
    except OSError:
        return []
    return entries


async def session_out(record: SessionRecord, supervisor: SessionSupervisor) -> SessionOut:
    process: PiRpcProcess | None = supervisor.active(record.id)
    try:
        entries, leaf_id = await get_entries(record, supervisor, None)
    except (RpcError, ApiProblem):
        entries, leaf_id = [], None
    message_count = sum(1 for entry in entries if entry.get("type") == "message")
    materialized = bool(record.session_file and Path(record.session_file).is_file())
    state = (
        ("running" if process.is_streaming else "idle")
        if process is not None
        else record.last_status
    )
    return SessionOut(
        id=record.id,
        name=record.name,
        cwd=record.cwd,
        provider=record.provider,
        model=record.model,
        thinking_level=record.thinking_level,
        system_prompt=record.system_prompt,
        system_prompt_mode=cast(SystemPromptMode, record.system_prompt_mode),
        session_file=record.session_file,
        materialized=materialized,
        state=state,
        is_streaming=bool(process and process.is_streaming),
        message_count=message_count,
        leaf_id=leaf_id,
        created_at=record.created_at,
        updated_at=record.updated_at,
        last_error=record.last_error,
    )


def encode_cursor(value: dict[str, str]) -> str:
    raw = json.dumps(value, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode_cursor(value: str) -> str:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(value + padding).decode()
