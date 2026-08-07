from __future__ import annotations

import asyncio
from collections.abc import Callable
from pathlib import Path
from typing import Any, TypeVar

from sqlalchemy import and_, create_engine, event, or_, select, text
from sqlalchemy.orm import Session, sessionmaker

from .files import inspect_session_file, utc_now
from .models import Base, SessionModel, SessionRecord, session_record

_Result = TypeVar("_Result")


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

    def utc_now(self) -> str:
        return utc_now()

    async def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.pi_session_dir.mkdir(parents=True, exist_ok=True)
        await asyncio.to_thread(Base.metadata.create_all, self.engine)
        await asyncio.to_thread(self._drop_legacy_mcp_tables)
        await self.reconcile_files()

    def _drop_legacy_mcp_tables(self) -> None:
        with self.engine.begin() as connection:
            connection.execute(text("DROP TABLE IF EXISTS session_mcp_servers"))
            connection.execute(text("DROP TABLE IF EXISTS mcp_servers"))

    async def _run(self, operation: Callable[[Session], _Result]) -> _Result:
        async with self._lock:
            return await asyncio.to_thread(lambda: self._transaction(operation))

    def _transaction(self, operation: Callable[[Session], _Result]) -> _Result:
        with self.sessions.begin() as db:
            return operation(db)

    async def create(
        self,
        *,
        session_id: str,
        name: str,
        cwd: str,
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
            return session_record(item)

        return await self._run(operation)

    async def get(self, session_id: str) -> SessionRecord | None:
        return await self._run(
            lambda db: (
                None if (item := db.get(SessionModel, session_id)) is None else session_record(item)
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
                session_record(item)
                for item in db.scalars(
                    stmt.order_by(
                        SessionModel.updated_at.desc(), SessionModel.id.desc()
                    ).limit(limit)
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
        model: str | None = None,
        thinking_level: str | None = None,
        update_thinking_level: bool = False,
    ) -> SessionRecord | None:
        values: dict[str, Any] = {}
        if model is not None:
            values["model"] = model
        if update_thinking_level:
            values["thinking_level"] = thinking_level
        return await self._update(session_id, **values)

    async def migrate_removed_models(
        self, allowed_models: set[str], *, default_model: str
    ) -> list[str]:
        def operation(db: Session) -> list[str]:
            items = list(
                db.scalars(select(SessionModel).where(SessionModel.model.not_in(allowed_models)))
            )
            for item in items:
                item.model = default_model
                item.updated_at = utc_now()
            return [item.id for item in items]

        return await self._run(operation)

    async def _update(self, session_id: str, **values: Any) -> SessionRecord | None:
        def operation(db: Session) -> SessionRecord | None:
            item = db.get(SessionModel, session_id)
            if item is None:
                return None
            for key, value in values.items():
                setattr(item, key, value)
            item.updated_at = utc_now()
            db.flush()
            return session_record(item)

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
        async with self._lock:
            def operation(db: Session) -> None:
                item = db.get(SessionModel, session_id)
                if item is None:
                    return
                item.last_status, item.last_error = status, error
                if session_file is not None:
                    item.session_file = session_file
                if touch:
                    item.updated_at = utc_now()

            await asyncio.to_thread(lambda: self._transaction(operation))

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
            result = session_record(item)
            db.delete(item)
            return result

        return await self._run(operation)

    async def reconcile_files(self) -> None:
        for path in sorted(self.pi_session_dir.glob("*.jsonl")):
            parsed = inspect_session_file(path)
            if parsed is None:
                continue
            existing = await self.get(str(parsed["id"]))
            if existing is None:
                created = await self.create(
                    session_id=str(parsed["id"]),
                    name=str(parsed["name"]),
                    cwd=str(parsed["cwd"]),
                    model=str(parsed["model"]),
                    thinking_level=parsed["thinking_level"],
                )
                await self.set_runtime(
                    created.id,
                    status=created.last_status,
                    error=created.last_error,
                    session_file=str(path),
                    touch=False,
                )
            elif existing.session_file != str(path):
                await self.set_runtime(
                    existing.id,
                    status=existing.last_status,
                    error=existing.last_error,
                    session_file=str(path),
                    touch=False,
                )
