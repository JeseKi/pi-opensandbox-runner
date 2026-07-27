from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TypeVar

from sqlalchemy import Integer, String, Text, and_, create_engine, event, or_, select
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker

_Result = TypeVar("_Result")


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


class Base(DeclarativeBase):
    pass


class SessionModel(Base):
    __tablename__ = "sessions"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    name: Mapped[str] = mapped_column(String, nullable=False)
    cwd: Mapped[str] = mapped_column(String, nullable=False)
    provider: Mapped[str] = mapped_column(String, nullable=False)
    model: Mapped[str] = mapped_column(String, nullable=False)
    thinking_level: Mapped[str | None] = mapped_column(String)
    system_prompt: Mapped[str | None] = mapped_column(Text)
    system_prompt_mode: Mapped[str] = mapped_column(String, nullable=False, default="append")
    session_file: Mapped[str | None] = mapped_column(String)
    created_at: Mapped[str] = mapped_column(String, nullable=False)
    updated_at: Mapped[str] = mapped_column(String, nullable=False, index=True)
    last_status: Mapped[str] = mapped_column(String, nullable=False)
    last_error: Mapped[str | None] = mapped_column(Text)
    event_seq: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


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


def _record(item: SessionModel) -> SessionRecord:
    return SessionRecord(**{key: getattr(item, key) for key in SessionRecord.__dataclass_fields__})


class Catalog:
    def __init__(self, path: Path, pi_session_dir: Path):
        self.path, self.pi_session_dir = path, pi_session_dir
        self._lock = asyncio.Lock()
        self.engine = create_engine(f"sqlite:///{path}", connect_args={"check_same_thread": False})
        event.listen(self.engine, "connect", self._configure_sqlite)
        self.sessions = sessionmaker(bind=self.engine, expire_on_commit=False)

    @staticmethod
    def _configure_sqlite(connection: Any, _: Any) -> None:
        cursor = connection.cursor()
        try:
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA busy_timeout=5000")
        finally:
            cursor.close()

    async def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.pi_session_dir.mkdir(parents=True, exist_ok=True)
        await asyncio.to_thread(Base.metadata.create_all, self.engine)
        await self.reconcile_files()

    async def _run(self, operation: Callable[[Session], _Result]) -> _Result:
        async with self._lock:

            def transaction() -> _Result:
                with self.sessions.begin() as db:
                    return operation(db)

            return await asyncio.to_thread(transaction)

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

        def operation(db: Session) -> SessionRecord:
            item = SessionModel(
                id=session_id,
                name=name,
                cwd=cwd,
                provider=provider,
                model=model,
                thinking_level=thinking_level,
                system_prompt=system_prompt,
                system_prompt_mode=system_prompt_mode,
                created_at=now,
                updated_at=now,
                last_status="stopped",
                last_error=None,
                event_seq=0,
            )
            db.add(item)
            db.flush()
            return _record(item)

        return await self._run(operation)

    async def get(self, session_id: str) -> SessionRecord | None:
        return await self._run(
            lambda db: (
                None if (item := db.get(SessionModel, session_id)) is None else _record(item)
            )
        )

    async def list_page(
        self, *, limit: int, before_updated_at: str | None = None, before_id: str | None = None
    ) -> list[SessionRecord]:
        def operation(db: Session) -> list[SessionRecord]:
            stmt = select(SessionModel)
            if before_updated_at is not None:
                stmt = stmt.where(
                    or_(
                        SessionModel.updated_at < before_updated_at,
                        and_(
                            SessionModel.updated_at == before_updated_at,
                            SessionModel.id < before_id,
                        ),
                    )
                )
            return [
                _record(item)
                for item in db.scalars(
                    stmt.order_by(SessionModel.updated_at.desc(), SessionModel.id.desc()).limit(
                        limit
                    )
                )
            ]

        return await self._run(operation)

    async def update_name(self, session_id: str, name: str) -> SessionRecord | None:
        return await self._update(session_id, name=name)

    async def update_system_prompt(
        self, session_id: str, *, system_prompt: str | None, system_prompt_mode: str = "append"
    ) -> SessionRecord | None:
        return await self._update(
            session_id, system_prompt=system_prompt, system_prompt_mode=system_prompt_mode
        )

    async def update_model_settings(
        self,
        session_id: str,
        *,
        provider: str | None = None,
        model: str | None = None,
        thinking_level: str | None = None,
        update_thinking_level: bool = False,
    ) -> SessionRecord | None:
        values: dict[str, Any] = {}
        if provider is not None and model is not None:
            values.update(provider=provider, model=model)
        if update_thinking_level:
            values["thinking_level"] = thinking_level
        return await self._update(session_id, **values)

    async def _update(self, session_id: str, **values: Any) -> SessionRecord | None:
        def operation(db: Session) -> SessionRecord | None:
            item = db.get(SessionModel, session_id)
            if item is None:
                return None
            for key, value in values.items():
                setattr(item, key, value)
            item.updated_at = utc_now()
            db.flush()
            return _record(item)

        return await self._run(operation)

    async def set_runtime(
        self,
        session_id: str,
        *,
        status: str,
        error: str | None = None,
        session_file: str | None = None,
        touch: bool = True,
    ) -> None:
        values: dict[str, Any] = {"last_status": status, "last_error": error}
        if session_file is not None:
            values["session_file"] = session_file
        if not touch:
            values["updated_at"] = None
        async with self._lock:

            def operation(db: Session) -> None:
                item = db.get(SessionModel, session_id)
                if item is None:
                    return
                for key, value in values.items():
                    if key != "updated_at":
                        setattr(item, key, value)
                if touch:
                    item.updated_at = utc_now()

            await asyncio.to_thread(lambda: self._transaction(operation))

    def _transaction(self, operation: Any) -> Any:
        with self.sessions.begin() as db:
            return operation(db)

    async def next_event_seq(self, session_id: str) -> int:
        def operation(db: Session) -> int:
            item = db.get(SessionModel, session_id)
            if item is None:
                raise KeyError(session_id)
            item.event_seq += 1
            db.flush()
            return item.event_seq

        return await self._run(operation)

    async def delete(self, session_id: str) -> SessionRecord | None:
        def operation(db: Session) -> SessionRecord | None:
            item = db.get(SessionModel, session_id)
            if item is None:
                return None
            result = _record(item)
            db.delete(item)
            return result

        return await self._run(operation)

    async def reconcile_files(self) -> None:
        for path in sorted(self.pi_session_dir.glob("*.jsonl")):
            parsed = _inspect_session_file(path)
            if parsed is None:
                continue
            existing = await self.get(str(parsed["id"]))
            if existing is None:
                await self.create(
                    session_id=str(parsed["id"]),
                    name=str(parsed["name"]),
                    cwd=str(parsed["cwd"]),
                    provider=str(parsed["provider"]),
                    model=str(parsed["model"]),
                    thinking_level=parsed["thinking_level"],
                )
            elif existing.session_file != str(path):
                await self.set_runtime(
                    existing.id,
                    status=existing.last_status,
                    error=existing.last_error,
                    session_file=str(path),
                    touch=False,
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
                provider, model = (
                    str(entry.get("provider") or provider),
                    str(entry.get("modelId") or model),
                )
            elif entry.get("type") == "thinking_level_change":
                thinking_level = str(entry.get("thinkingLevel") or "") or None
            elif (
                entry.get("type") == "message"
                and isinstance(entry.get("message"), dict)
                and entry["message"].get("role") == "assistant"
            ):
                provider, model = (
                    str(entry["message"].get("provider") or provider),
                    str(entry["message"].get("model") or model),
                )
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
        "provider": provider,
        "model": model,
        "thinking_level": thinking_level,
        "created_at": created,
        "updated_at": updated_at or created,
    }
