#!/usr/bin/env python3
"""Small line-delimited RPC double used by integration tests."""

from __future__ import annotations

import json
import signal
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


def argument(name: str) -> str | None:
    try:
        return sys.argv[sys.argv.index(name) + 1]
    except (ValueError, IndexError):
        return None


session_dir = Path(argument("--session-dir") or ".")
session_dir.mkdir(parents=True, exist_ok=True)
session_file_arg = argument("--session")
session_id = argument("--session-id") or "restored"
session_file = (
    Path(session_file_arg)
    if session_file_arg
    else session_dir / f"{session_id}.jsonl"
)
session_name = argument("--name") or session_id
provider = argument("--provider") or "fake"
model = argument("--model") or "fake-model"
entries: list[dict[str, Any]] = []

if session_file.is_file():
    for raw in session_file.read_text(encoding="utf-8").splitlines():
        item = json.loads(raw)
        if item.get("type") == "session":
            session_id = str(item["id"])
        elif item.get("type") == "session_info":
            session_name = str(item.get("name") or session_name)
        else:
            entries.append(item)


def emit(value: dict[str, Any]) -> None:
    print(json.dumps(value, separators=(",", ":")), flush=True)


def persist() -> None:
    header = {
        "type": "session",
        "id": session_id,
        "timestamp": datetime.now(UTC).isoformat(),
        "cwd": str(Path.cwd()),
    }
    info = {"type": "session_info", "id": "name", "name": session_name}
    lines = [header, info, *entries]
    session_file.write_text(
        "".join(json.dumps(item, separators=(",", ":")) + "\n" for item in lines),
        encoding="utf-8",
    )


def respond(command: dict[str, Any], data: dict[str, Any] | None = None) -> None:
    emit(
        {
            "type": "response",
            "id": command["id"],
            "success": True,
            "data": data or {},
        }
    )


signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))

for line in sys.stdin:
    command = json.loads(line)
    command_type = command["type"]
    if command_type == "get_state":
        respond(
            command,
            {
                "sessionId": session_id,
                "sessionName": session_name,
                "sessionFile": str(session_file),
                "isStreaming": False,
            },
        )
    elif command_type == "set_session_name":
        session_name = command["name"]
        if session_file.exists():
            persist()
        respond(command)
    elif command_type == "set_model":
        provider = command["provider"]
        model = command["modelId"]
        persist()
        respond(command)
    elif command_type in {"set_thinking_level", "abort"}:
        respond(command)
    elif command_type in {"prompt", "steer", "follow_up"}:
        user_id = f"u{len(entries) + 1}"
        assistant_id = f"a{len(entries) + 2}"
        entries.extend(
            [
                {
                    "type": "message",
                    "id": user_id,
                    "message": {"role": "user", "content": command["message"]},
                },
                {
                    "type": "message",
                    "id": assistant_id,
                    "parentId": user_id,
                    "message": {
                        "role": "assistant",
                        "content": [{"type": "text", "text": f"echo: {command['message']}"}],
                        "provider": provider,
                        "model": model,
                    },
                },
            ]
        )
        persist()
        respond(command)
        emit({"type": "agent_start"})
        emit({"type": "message_end", "message": entries[-1]})
        emit({"type": "agent_end"})
    elif command_type == "get_entries":
        selected = entries
        since = command.get("since")
        if since is not None:
            matching = [index for index, item in enumerate(entries) if item.get("id") == since]
            if not matching:
                emit(
                    {
                        "type": "response",
                        "id": command["id"],
                        "success": False,
                        "error": f"Entry not found: {since}",
                    }
                )
                continue
            selected = entries[matching[0] + 1 :]
        respond(
            command,
            {
                "entries": selected,
                "leafId": entries[-1]["id"] if entries else None,
            },
        )
    else:
        emit(
            {
                "type": "response",
                "id": command["id"],
                "success": False,
                "error": f"unsupported: {command_type}",
            }
        )
