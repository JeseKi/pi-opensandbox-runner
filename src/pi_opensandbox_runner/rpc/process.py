from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import time
import uuid
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from ..catalog import SessionRecord
from ..config import Settings
from ..mcp import McpRuntimeConfig, write_runtime_config
from .errors import RpcError, RpcProcessExited

logger = logging.getLogger(__name__)

EventHandler = Callable[[dict[str, Any]], Awaitable[None]]
ExitHandler = Callable[[int | None, bool], Awaitable[None]]


def _model_catalog_fingerprint(path: Path) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return "missing"


class PiRpcProcess:
    def __init__(
        self,
        *,
        session_id: str,
        process: asyncio.subprocess.Process,
        timeout: float,
        system_prompt_config: tuple[str | None, str],
        mcp_config_fingerprint: str,
        model_catalog_fingerprint: str,
        on_event: EventHandler,
        on_exit: ExitHandler,
    ):
        self.session_id = session_id
        self.process = process
        self.timeout = timeout
        self.system_prompt_config = system_prompt_config
        self.mcp_config_fingerprint = mcp_config_fingerprint
        self.model_catalog_fingerprint = model_catalog_fingerprint
        self.on_event = on_event
        self.on_exit = on_exit
        self.pending: dict[str, asyncio.Future[dict[str, Any]]] = {}
        self.write_lock = asyncio.Lock()
        self.last_activity = time.monotonic()
        self.is_streaming = False
        self.expected_stop = False
        self._stdout_task = asyncio.create_task(self._read_stdout())
        self._stderr_task = asyncio.create_task(self._read_stderr())

    @classmethod
    async def start(
        cls,
        record: SessionRecord,
        settings: Settings,
        *,
        mcp_config: McpRuntimeConfig,
        on_event: EventHandler,
        on_exit: ExitHandler,
    ) -> PiRpcProcess:
        cwd = Path(record.cwd)
        cwd.mkdir(parents=True, exist_ok=True)
        args = [
            settings.pi_executable,
            "--mode",
            "rpc",
            "--session-dir",
            str(settings.pi_session_dir),
            "--no-approve",
        ]
        if record.session_file and Path(record.session_file).is_file():
            args.extend(["--session", record.session_file])
        else:
            args.extend(
                [
                    "--session-id",
                    record.id,
                    "--provider",
                    "litellm",
                    "--model",
                    record.model,
                    "--name",
                    record.name,
                ]
            )
        if record.system_prompt is not None:
            prompt_flag = (
                "--system-prompt"
                if record.system_prompt_mode == "replace"
                else "--append-system-prompt"
            )
            args.extend([prompt_flag, record.system_prompt])
        config_path: Path | None = None
        if mcp_config.contents != '{"mcpServers":{}}':
            config_path = settings.state_root / "mcp" / f"{record.id}.json"
            write_runtime_config(config_path, mcp_config)
            args.extend(["--extension", "/opt/pi-runner-mcp/src/index.ts"])
        environment = os.environ.copy()
        environment["HOME"] = "/root"
        environment["PI_CODING_AGENT_SESSION_DIR"] = str(settings.pi_session_dir)
        if config_path is not None:
            environment["PI_RUNNER_MCP_CONFIG"] = str(config_path)
        process = await asyncio.create_subprocess_exec(
            *args,
            cwd=cwd,
            env=environment,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            limit=16 * 1024 * 1024,
        )
        instance = cls(
            session_id=record.id,
            process=process,
            timeout=settings.rpc_timeout_seconds,
            system_prompt_config=(record.system_prompt, record.system_prompt_mode),
            mcp_config_fingerprint=mcp_config.fingerprint,
            model_catalog_fingerprint=_model_catalog_fingerprint(settings.model_catalog_path),
            on_event=on_event,
            on_exit=on_exit,
        )
        state_response = await instance.request({"type": "get_state"})
        state = state_response.get("data")
        if not isinstance(state, dict):
            raise RpcError("Pi get_state returned invalid data")
        current_name = state.get("sessionName")
        if current_name != record.name:
            await instance.request({"type": "set_session_name", "name": record.name})
        if record.thinking_level and not record.session_file:
            await instance.request({"type": "set_thinking_level", "level": record.thinking_level})
        instance.is_streaming = bool(state.get("isStreaming"))
        return instance

    @property
    def alive(self) -> bool:
        return self.process.returncode is None

    @property
    def idle(self) -> bool:
        return self.alive and not self.is_streaming

    async def request(self, command: dict[str, Any]) -> dict[str, Any]:
        if not self.alive or self.process.stdin is None:
            raise RpcProcessExited("Pi RPC process is not running")
        request_id = str(command.get("id") or uuid.uuid4())
        payload = dict(command)
        payload["id"] = request_id
        loop = asyncio.get_running_loop()
        future: asyncio.Future[dict[str, Any]] = loop.create_future()
        self.pending[request_id] = future
        encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode() + b"\n"
        try:
            async with self.write_lock:
                self.process.stdin.write(encoded)
                await self.process.stdin.drain()
            response = await asyncio.wait_for(future, timeout=self.timeout)
        except BaseException:
            self.pending.pop(request_id, None)
            raise
        self.last_activity = time.monotonic()
        if not response.get("success"):
            raise RpcError(str(response.get("error") or "Pi RPC request failed"))
        return response

    async def stop(self, grace_seconds: float) -> None:
        if not self.alive:
            return
        self.expected_stop = True
        self.process.terminate()
        try:
            await asyncio.wait_for(self.process.wait(), timeout=grace_seconds)
        except TimeoutError:
            self.process.kill()
            await self.process.wait()
        await asyncio.gather(self._stdout_task, self._stderr_task, return_exceptions=True)

    async def _read_stdout(self) -> None:
        assert self.process.stdout is not None
        try:
            while True:
                raw = await self.process.stdout.readline()
                if not raw:
                    break
                if raw.endswith(b"\n"):
                    raw = raw[:-1]
                if raw.endswith(b"\r"):
                    raw = raw[:-1]
                if not raw:
                    continue
                try:
                    record = json.loads(raw)
                except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                    logger.warning("invalid Pi RPC output for %s: %s", self.session_id, exc)
                    continue
                if not isinstance(record, dict):
                    continue
                self.last_activity = time.monotonic()
                if record.get("type") == "response":
                    request_id = record.get("id")
                    future = self.pending.pop(str(request_id), None)
                    if future is not None and not future.done():
                        future.set_result(record)
                    continue
                event_type = record.get("type")
                if event_type == "agent_start":
                    self.is_streaming = True
                elif event_type in {"agent_settled", "agent_end"}:
                    self.is_streaming = False
                await self.on_event(record)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Pi RPC stdout reader failed for %s", self.session_id)
        finally:
            error = RpcProcessExited("Pi RPC process exited")
            for future in self.pending.values():
                if not future.done():
                    future.set_exception(error)
            self.pending.clear()
            return_code = await self.process.wait()
            await self.on_exit(return_code, self.expected_stop)

    async def _read_stderr(self) -> None:
        assert self.process.stderr is not None
        while True:
            line = await self.process.stderr.readline()
            if not line:
                return
            logger.info("pi[%s] %s", self.session_id, line.decode(errors="replace").rstrip())
