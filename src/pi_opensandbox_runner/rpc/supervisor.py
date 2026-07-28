from __future__ import annotations

import asyncio
import time
from contextlib import suppress
from typing import Any

from ..catalog import Catalog, SessionRecord
from ..config import Settings
from ..journal import EventJournal
from ..mcp import build_runtime_config, missing_environment
from .errors import McpEnvironmentMissing, RpcError, SessionCapacityExceeded
from .process import PiRpcProcess, _model_catalog_fingerprint


class SessionSupervisor:
    def __init__(
        self,
        settings: Settings,
        catalog: Catalog,
        journal: EventJournal,
    ):
        self.settings = settings
        self.catalog = catalog
        self.journal = journal
        self.processes: dict[str, PiRpcProcess] = {}
        self._session_locks: dict[str, asyncio.Lock] = {}
        self._command_locks: dict[str, asyncio.Lock] = {}
        self._map_lock = asyncio.Lock()
        self._reaper_task: asyncio.Task[None] | None = None
        self._closing = False

    async def start(self) -> None:
        self._reaper_task = asyncio.create_task(self._reaper())

    async def close(self) -> None:
        self._closing = True
        if self._reaper_task is not None:
            self._reaper_task.cancel()
            await asyncio.gather(self._reaper_task, return_exceptions=True)
        for session_id in list(self.processes):
            await self.stop(session_id, abort=False)

    def _session_lock(self, session_id: str) -> asyncio.Lock:
        return self._session_locks.setdefault(session_id, asyncio.Lock())

    def command_lock(self, session_id: str) -> asyncio.Lock:
        return self._command_locks.setdefault(session_id, asyncio.Lock())

    def active(self, session_id: str) -> PiRpcProcess | None:
        process = self.processes.get(session_id)
        return process if process is not None and process.alive else None

    async def needs_configuration_restart(self, record: SessionRecord) -> bool:
        process = self.active(record.id)
        if process is None:
            return False
        if process.system_prompt_config != (record.system_prompt, record.system_prompt_mode):
            return True
        if process.model_catalog_fingerprint != _model_catalog_fingerprint(
            self.settings.model_catalog_path
        ):
            return True
        servers = await self.catalog.get_session_mcp_servers(record.id)
        assert servers is not None
        return process.mcp_config_fingerprint != build_runtime_config(servers).fingerprint

    async def get_or_start(self, record: SessionRecord) -> PiRpcProcess:
        async with self._session_lock(record.id):
            current = self.active(record.id)
            if current is not None:
                return current
            await self._reserve_capacity()
            await self.catalog.set_runtime(record.id, status="starting", error=None)
            servers = await self.catalog.get_session_mcp_servers(record.id)
            assert servers is not None
            mcp_config = build_runtime_config(servers)
            if missing := missing_environment(mcp_config):
                await self.catalog.set_runtime(record.id, status="stopped", error=None)
                raise McpEnvironmentMissing(missing)

            async def on_event(event: dict[str, Any]) -> None:
                await self.journal.append(record.id, "pi", event)
                event_type = event.get("type")
                if event_type == "agent_start":
                    await self.catalog.set_runtime(record.id, status="running", error=None)
                elif event_type in {"agent_settled", "agent_end"}:
                    await self.catalog.set_runtime(record.id, status="idle", error=None)

            async def on_exit(return_code: int | None, expected: bool) -> None:
                await self._process_exited(record.id, return_code, expected)

            try:
                process = await PiRpcProcess.start(
                    record,
                    self.settings,
                    mcp_config=mcp_config,
                    on_event=on_event,
                    on_exit=on_exit,
                )
            except Exception as exc:
                await self.catalog.set_runtime(record.id, status="failed", error=str(exc))
                raise
            async with self._map_lock:
                self.processes[record.id] = process
            state = await process.request({"type": "get_state"})
            raw_data = state.get("data")
            data: dict[str, Any] = raw_data if isinstance(raw_data, dict) else {}
            session_file = data.get("sessionFile")
            await self.catalog.set_runtime(
                record.id,
                status="running" if process.is_streaming else "idle",
                error=None,
                session_file=str(session_file) if session_file else None,
            )
            await self.journal.append(
                record.id,
                "bridge",
                {"type": "process_started", "pid": process.process.pid},
            )
            return process

    async def _reserve_capacity(self) -> None:
        async with self._map_lock:
            active = [process for process in self.processes.values() if process.alive]
            if len(active) < self.settings.max_active_sessions:
                return
            idle = sorted(
                (process for process in active if process.idle),
                key=lambda process: process.last_activity,
            )
        if not idle:
            raise SessionCapacityExceeded("all Pi session slots are busy")
        await self.stop(idle[0].session_id, abort=False)

    async def stop(self, session_id: str, *, abort: bool) -> bool:
        async with self._session_lock(session_id):
            process = self.active(session_id)
            if process is None:
                return False
            await self.catalog.set_runtime(session_id, status="stopping", error=None)
            if abort and process.is_streaming:
                with suppress(RpcError):
                    await process.request({"type": "abort"})
            await process.stop(self.settings.stop_grace_seconds)
            async with self._map_lock:
                self.processes.pop(session_id, None)
            await self.catalog.set_runtime(session_id, status="stopped", error=None)
            await self.journal.append(session_id, "bridge", {"type": "process_stopped"})
            return True

    async def _process_exited(
        self, session_id: str, return_code: int | None, expected: bool
    ) -> None:
        async with self._map_lock:
            current = self.processes.get(session_id)
            if current is not None and current.process.returncode is not None:
                self.processes.pop(session_id, None)
        if self._closing:
            return
        status = "stopped" if expected else "failed"
        error = None if expected else f"Pi RPC process exited with code {return_code}"
        await self.catalog.set_runtime(session_id, status=status, error=error)
        with suppress(KeyError):
            await self.journal.append(
                session_id,
                "bridge",
                {
                    "type": "process_exited",
                    "return_code": return_code,
                    "expected": expected,
                },
            )

    async def _reaper(self) -> None:
        interval = max(1.0, min(30.0, self.settings.idle_timeout_seconds / 2))
        while True:
            await asyncio.sleep(interval)
            cutoff = time.monotonic() - self.settings.idle_timeout_seconds
            for process in list(self.processes.values()):
                if process.idle and process.last_activity < cutoff:
                    await self.stop(process.session_id, abort=False)
