from __future__ import annotations

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
