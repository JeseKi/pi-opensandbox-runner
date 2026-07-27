from __future__ import annotations

import asyncio
import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


@dataclass(frozen=True)
class SessionRecord:
    id: str
    name: str
    cwd: str
    provider: str
    model: str
    thinking_level: str | None
    system_prompt: str | None
    system_prompt_mode: str
    session_file: str | None
    created_at: str
    updated_at: str
    last_status: str
    last_error: str | None
    event_seq: int


class Catalog:
    def __init__(self, path: Path, pi_session_dir: Path):
        self.path = path
        self.pi_session_dir = pi_session_dir
        self._lock = asyncio.Lock()

    async def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.pi_session_dir.mkdir(parents=True, exist_ok=True)
        async with self._lock:
            with self._connect() as db:
                db.executescript(
                    """
                    PRAGMA journal_mode=WAL;
                    CREATE TABLE IF NOT EXISTS sessions (
                        id TEXT PRIMARY KEY,
                        name TEXT NOT NULL,
                        cwd TEXT NOT NULL,
                        provider TEXT NOT NULL,
                        model TEXT NOT NULL,
                        thinking_level TEXT,
                        system_prompt TEXT,
                        system_prompt_mode TEXT NOT NULL DEFAULT 'append',
                        session_file TEXT,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        last_status TEXT NOT NULL,
                        last_error TEXT,
                        event_seq INTEGER NOT NULL DEFAULT 0
                    );
                    CREATE INDEX IF NOT EXISTS sessions_updated
                    ON sessions(updated_at DESC, id DESC);
                    """
                )
                self._ensure_session_columns(db)
        await self.reconcile_files()

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path)
        db.row_factory = sqlite3.Row
        return db

    @staticmethod
    def _ensure_session_columns(db: sqlite3.Connection) -> None:
        columns = {row["name"] for row in db.execute("PRAGMA table_info(sessions)")}
        if "system_prompt" not in columns:
            db.execute("ALTER TABLE sessions ADD COLUMN system_prompt TEXT")
        if "system_prompt_mode" not in columns:
            db.execute(
                "ALTER TABLE sessions ADD COLUMN system_prompt_mode TEXT NOT NULL DEFAULT 'append'"
            )

    @staticmethod
    def _record(row: sqlite3.Row) -> SessionRecord:
        return SessionRecord(**dict(row))

    async def create(
        self,
        *,
        session_id: str,
        name: str,
        cwd: str,
        provider: str,
        model: str,
        thinking_level: str | None,
        system_prompt: str | None = None,
        system_prompt_mode: str = "append",
    ) -> SessionRecord:
        now = utc_now()
        async with self._lock:
            with self._connect() as db:
                db.execute(
                    """
                    INSERT INTO sessions
                    (id, name, cwd, provider, model, thinking_level, system_prompt,
                     system_prompt_mode, session_file,
                     created_at, updated_at, last_status, last_error, event_seq)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, 'stopped', NULL, 0)
                    """,
                    (
                        session_id,
                        name,
                        cwd,
                        provider,
                        model,
                        thinking_level,
                        system_prompt,
                        system_prompt_mode,
                        now,
                        now,
                    ),
                )
                row = db.execute("SELECT * FROM sessions WHERE id = ?", (session_id,)).fetchone()
        assert row is not None
        return self._record(row)

    async def get(self, session_id: str) -> SessionRecord | None:
        async with self._lock:
            with self._connect() as db:
                row = db.execute("SELECT * FROM sessions WHERE id = ?", (session_id,)).fetchone()
        return None if row is None else self._record(row)

    async def list_page(
        self,
        *,
        limit: int,
        before_updated_at: str | None = None,
        before_id: str | None = None,
    ) -> list[SessionRecord]:
        async with self._lock:
            with self._connect() as db:
                if before_updated_at is None:
                    rows = db.execute(
                        "SELECT * FROM sessions ORDER BY updated_at DESC, id DESC LIMIT ?",
                        (limit,),
                    ).fetchall()
                else:
                    rows = db.execute(
                        """
                        SELECT * FROM sessions
                        WHERE updated_at < ?
                           OR (updated_at = ? AND id < ?)
                        ORDER BY updated_at DESC, id DESC LIMIT ?
                        """,
                        (before_updated_at, before_updated_at, before_id, limit),
                    ).fetchall()
        return [self._record(row) for row in rows]

    async def update_name(self, session_id: str, name: str) -> SessionRecord | None:
        now = utc_now()
        async with self._lock:
            with self._connect() as db:
                result = db.execute(
                    "UPDATE sessions SET name = ?, updated_at = ? WHERE id = ?",
                    (name, now, session_id),
                )
                if result.rowcount == 0:
                    return None
                row = db.execute("SELECT * FROM sessions WHERE id = ?", (session_id,)).fetchone()
        assert row is not None
        return self._record(row)

    async def update_model_settings(
        self,
        session_id: str,
        *,
        provider: str | None = None,
        model: str | None = None,
        thinking_level: str | None = None,
        update_thinking_level: bool = False,
    ) -> SessionRecord | None:
        """Persist the model state selected through Pi RPC for later resumes."""
        fields: list[str] = ["updated_at = ?"]
        values: list[str | None] = [utc_now()]
        if provider is not None and model is not None:
            fields.extend(["provider = ?", "model = ?"])
            values.extend([provider, model])
        if update_thinking_level:
            fields.append("thinking_level = ?")
            values.append(thinking_level)
        values.append(session_id)
        async with self._lock:
            with self._connect() as db:
                result = db.execute(
                    f"UPDATE sessions SET {', '.join(fields)} WHERE id = ?", values
                )
                if result.rowcount == 0:
                    return None
                row = db.execute("SELECT * FROM sessions WHERE id = ?", (session_id,)).fetchone()
        assert row is not None
        return self._record(row)

    async def update_system_prompt(
        self,
        session_id: str,
        *,
        system_prompt: str | None,
        system_prompt_mode: str = "append",
    ) -> SessionRecord | None:
        now = utc_now()
        async with self._lock:
            with self._connect() as db:
                result = db.execute(
                    """
                    UPDATE sessions
                    SET system_prompt = ?, system_prompt_mode = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (system_prompt, system_prompt_mode, now, session_id),
                )
                if result.rowcount == 0:
                    return None
                row = db.execute("SELECT * FROM sessions WHERE id = ?", (session_id,)).fetchone()
        assert row is not None
        return self._record(row)

    async def set_runtime(
        self,
        session_id: str,
        *,
        status: str,
        error: str | None = None,
        session_file: str | None = None,
        touch: bool = True,
    ) -> None:
        fields = ["last_status = ?", "last_error = ?"]
        values: list[Any] = [status, error]
        if session_file is not None:
            fields.append("session_file = ?")
            values.append(session_file)
        if touch:
            fields.append("updated_at = ?")
            values.append(utc_now())
        values.append(session_id)
        async with self._lock:
            with self._connect() as db:
                db.execute(
                    f"UPDATE sessions SET {', '.join(fields)} WHERE id = ?",  # noqa: S608
                    values,
                )

    async def next_event_seq(self, session_id: str) -> int:
        async with self._lock:
            with self._connect() as db:
                db.execute(
                    "UPDATE sessions SET event_seq = event_seq + 1 WHERE id = ?",
                    (session_id,),
                )
                row = db.execute(
                    "SELECT event_seq FROM sessions WHERE id = ?", (session_id,)
                ).fetchone()
        if row is None:
            raise KeyError(session_id)
        return int(row["event_seq"])

    async def delete(self, session_id: str) -> SessionRecord | None:
        async with self._lock:
            with self._connect() as db:
                row = db.execute("SELECT * FROM sessions WHERE id = ?", (session_id,)).fetchone()
                if row is None:
                    return None
                db.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
        return self._record(row)

    async def reconcile_files(self) -> None:
        for path in sorted(self.pi_session_dir.glob("*.jsonl")):
            parsed = _inspect_session_file(path)
            if parsed is None:
                continue
            existing = await self.get(str(parsed["id"]))
            if existing is not None:
                if existing.session_file != str(path):
                    await self.set_runtime(
                        existing.id,
                        status=existing.last_status,
                        error=existing.last_error,
                        session_file=str(path),
                        touch=False,
                    )
                continue
            async with self._lock:
                with self._connect() as db:
                    db.execute(
                        """
                        INSERT OR IGNORE INTO sessions
                        (id, name, cwd, provider, model, thinking_level, system_prompt,
                         system_prompt_mode, session_file,
                         created_at, updated_at, last_status, last_error, event_seq)
                        VALUES (?, ?, ?, ?, ?, ?, NULL, 'append', ?, ?, ?, 'stopped', NULL, 0)
                        """,
                        (
                            parsed["id"],
                            parsed["name"],
                            parsed["cwd"],
                            parsed["provider"],
                            parsed["model"],
                            parsed["thinking_level"],
                            str(path),
                            parsed["created_at"],
                            parsed["updated_at"],
                        ),
                    )


def inspect_session_file(path: Path) -> dict[str, str | None] | None:
    return _inspect_session_file(path)


def _inspect_session_file(path: Path) -> dict[str, str | None] | None:
    header: dict[str, Any] | None = None
    name: str | None = None
    provider = "unknown"
    model = "unknown"
    thinking_level: str | None = None
    updated_at: str | None = None
    try:
        with path.open(encoding="utf-8") as stream:
            for raw in stream:
                try:
                    entry = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if not isinstance(entry, dict):
                    continue
                if header is None and entry.get("type") == "session":
                    header = entry
                if entry.get("type") == "session_info":
                    candidate = entry.get("name")
                    if isinstance(candidate, str) and candidate.strip():
                        name = candidate.strip()
                elif entry.get("type") == "model_change":
                    provider = str(entry.get("provider") or provider)
                    model = str(entry.get("modelId") or model)
                elif entry.get("type") == "thinking_level_change":
                    thinking_level = str(entry.get("thinkingLevel") or "") or None
                elif entry.get("type") == "message":
                    message = entry.get("message")
                    if isinstance(message, dict) and message.get("role") == "assistant":
                        provider = str(message.get("provider") or provider)
                        model = str(message.get("model") or model)
                timestamp = entry.get("timestamp")
                if isinstance(timestamp, str):
                    updated_at = timestamp
    except OSError:
        return None
    if header is None or not isinstance(header.get("id"), str):
        return None
    created = str(header.get("timestamp") or utc_now())
    return {
        "id": header["id"],
        "name": name or header["id"],
        "cwd": str(header.get("cwd") or "/root/workspace"),
        "provider": provider,
        "model": model,
        "thinking_level": thinking_level,
        "created_at": created,
        "updated_at": updated_at or created,
    }
