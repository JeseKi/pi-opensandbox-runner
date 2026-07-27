from __future__ import annotations

from pathlib import Path

import pytest

from pi_opensandbox_runner.catalog import Catalog
from pi_opensandbox_runner.journal import EventCursorExpired, EventJournal


@pytest.mark.asyncio
async def test_event_cursor_replay_and_expiry(tmp_path: Path) -> None:
    catalog = Catalog(tmp_path / "bridge.db", tmp_path / "pi")
    await catalog.initialize()
    await catalog.create(
        session_id="session",
        name="events",
        cwd=str(tmp_path),
        provider="test",
        model="test",
        thinking_level=None,
    )
    journal = EventJournal(
        tmp_path / "events",
        catalog,
        segment_bytes=1,
        segment_count=2,
    )
    await journal.append("session", "bridge", {"type": "one"})
    await journal.append("session", "pi", {"type": "two"})
    await journal.append("session", "pi", {"type": "three"})

    replay = await journal.read_after("session", 1)
    assert [item["seq"] for item in replay] == [2, 3]
    assert replay[0]["source"] == "pi"
    with pytest.raises(EventCursorExpired):
        await journal.ensure_cursor("session", 0)
