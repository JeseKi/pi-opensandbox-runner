from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


def inspect_session_file(path: Path) -> dict[str, str | None] | None:
    header: dict[str, Any] | None = None
    name: str | None = None
    model = "unknown"
    thinking_level: str | None = None
    updated_at: str | None = None
    try:
        for raw in path.read_text(encoding="utf-8").splitlines():
            try:
                entry = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if not isinstance(entry, dict):
                continue
            if header is None and entry.get("type") == "session":
                header = entry
            if entry.get("type") == "session_info":
                name = str(entry.get("name") or name or "") or None
            elif entry.get("type") == "model_change":
                model = str(entry.get("modelId") or model)
            elif entry.get("type") == "thinking_level_change":
                thinking_level = str(entry.get("thinkingLevel") or "") or None
            elif (
                entry.get("type") == "message"
                and isinstance(entry.get("message"), dict)
                and entry["message"].get("role") == "assistant"
            ):
                model = str(entry["message"].get("model") or model)
            if isinstance(entry.get("timestamp"), str):
                updated_at = entry["timestamp"]
    except OSError:
        return None
    if header is None or not isinstance(header.get("id"), str):
        return None
    created = str(header.get("timestamp") or utc_now())
    return {
        "id": header["id"],
        "name": name or header["id"],
        "cwd": str(header.get("cwd") or "/root/workspace"),
        "model": model,
        "thinking_level": thinking_level,
        "created_at": created,
        "updated_at": updated_at or created,
    }
