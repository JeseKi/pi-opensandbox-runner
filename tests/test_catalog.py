from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from pi_opensandbox_runner.catalog import Catalog


@pytest.mark.asyncio
async def test_empty_logical_session_survives_catalog_restart(tmp_path: Path) -> None:
    database = tmp_path / "state" / "bridge.db"
    session_dir = tmp_path / "sessions"
    catalog = Catalog(database, session_dir)
    await catalog.initialize()
    created = await catalog.create(
        session_id="logical",
        name="empty",
        cwd=str(tmp_path / "workspace"),
        provider="test",
        model="test",
        thinking_level=None,
    )
    assert created.session_file is None

    restarted = Catalog(database, session_dir)
    await restarted.initialize()
    restored = await restarted.get("logical")
    assert restored is not None
    assert restored.name == "empty"
    assert restored.last_status == "stopped"


@pytest.mark.asyncio
async def test_legacy_catalog_migrates_system_prompt_columns(tmp_path: Path) -> None:
    database = tmp_path / "state" / "bridge.db"
    database.parent.mkdir()
    with sqlite3.connect(database) as db:
        db.executescript(
            """
            CREATE TABLE sessions (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                cwd TEXT NOT NULL,
                provider TEXT NOT NULL,
                model TEXT NOT NULL,
                thinking_level TEXT,
                session_file TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                last_status TEXT NOT NULL,
                last_error TEXT,
                event_seq INTEGER NOT NULL DEFAULT 0
            );
            INSERT INTO sessions VALUES
            ('legacy', 'old', '/workspace', 'test', 'model', NULL, NULL,
             '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00',
             'stopped', NULL, 0);
            """
        )

    catalog = Catalog(database, tmp_path / "sessions")
    await catalog.initialize()
    restored = await catalog.get("legacy")
    assert restored is not None
    assert restored.system_prompt is None
    assert restored.system_prompt_mode == "append"
