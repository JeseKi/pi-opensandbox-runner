from __future__ import annotations

import asyncio
import json
import shutil
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .catalog import Catalog


class EventCursorExpired(ValueError):
    def __init__(self, oldest_cursor: int):
        super().__init__("event cursor has expired")
        self.oldest_cursor = oldest_cursor


class EventJournal:
    def __init__(
        self,
        root: Path,
        catalog: Catalog,
        *,
        segment_bytes: int,
        segment_count: int,
    ):
        self.root = root
        self.catalog = catalog
        self.segment_bytes = segment_bytes
        self.segment_count = segment_count
        self._locks: dict[str, asyncio.Lock] = {}
        self._conditions: dict[str, asyncio.Condition] = {}

    def _lock(self, session_id: str) -> asyncio.Lock:
        return self._locks.setdefault(session_id, asyncio.Lock())

    def _condition(self, session_id: str) -> asyncio.Condition:
        return self._conditions.setdefault(session_id, asyncio.Condition())

    def _dir(self, session_id: str) -> Path:
        return self.root / session_id

    async def append(self, session_id: str, source: str, event: dict[str, Any]) -> dict[str, Any]:
        async with self._lock(session_id):
            seq = await self.catalog.next_event_seq(session_id)
            envelope = {
                "seq": seq,
                "session_id": session_id,
                "timestamp": datetime.now(UTC).isoformat(),
                "source": source,
                "event": event,
            }
            directory = self._dir(session_id)
            directory.mkdir(parents=True, exist_ok=True)
            segments = sorted(directory.glob("*.ndjson"))
            target = segments[-1] if segments else directory / f"{seq:020d}.ndjson"
            encoded = json.dumps(envelope, ensure_ascii=False, separators=(",", ":")) + "\n"
            projected_size = (
                target.stat().st_size if target.exists() else 0
            ) + len(encoded.encode())
            if target.exists() and projected_size > self.segment_bytes:
                target = directory / f"{seq:020d}.ndjson"
            with target.open("a", encoding="utf-8") as stream:
                stream.write(encoded)
            segments = sorted(directory.glob("*.ndjson"))
            for old in segments[: -self.segment_count]:
                old.unlink(missing_ok=True)
        condition = self._condition(session_id)
        async with condition:
            condition.notify_all()
        return envelope

    async def ensure_cursor(self, session_id: str, cursor: int) -> None:
        oldest = await self.oldest_cursor(session_id)
        if oldest is not None and cursor < oldest - 1:
            raise EventCursorExpired(oldest)

    async def oldest_cursor(self, session_id: str) -> int | None:
        segments = sorted(self._dir(session_id).glob("*.ndjson"))
        if not segments:
            return None
        try:
            return int(segments[0].stem)
        except ValueError:
            return None

    async def read_after(self, session_id: str, cursor: int) -> list[dict[str, Any]]:
        await self.ensure_cursor(session_id, cursor)
        items: list[dict[str, Any]] = []
        for path in sorted(self._dir(session_id).glob("*.ndjson")):
            try:
                with path.open(encoding="utf-8") as stream:
                    for line in stream:
                        try:
                            item = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        if isinstance(item, dict) and int(item.get("seq", 0)) > cursor:
                            items.append(item)
            except OSError:
                continue
        return items

    async def wait_for_change(self, session_id: str, timeout: float) -> None:
        condition = self._condition(session_id)
        async with condition:
            try:
                await asyncio.wait_for(condition.wait(), timeout)
            except TimeoutError:
                return

    async def delete(self, session_id: str) -> None:
        async with self._lock(session_id):
            shutil.rmtree(self._dir(session_id), ignore_errors=True)
