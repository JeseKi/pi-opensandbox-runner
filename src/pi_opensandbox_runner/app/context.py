from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path
from typing import Any

from fastapi import Response

from ..catalog import Catalog, SessionRecord
from ..config import Settings
from ..execd import ExecdClient, ExecdError
from ..journal import EventJournal
from ..model_catalog import ModelCatalog
from ..rpc import SessionSupervisor
from .problems import ApiProblem


class BridgeContext:
    max_text_file_bytes = 1_048_576

    def __init__(
        self,
        settings: Settings,
        catalog: Catalog,
        journal: EventJournal,
        supervisor: SessionSupervisor,
        execd: ExecdClient,
        model_catalog: ModelCatalog,
    ):
        self.settings = settings
        self.catalog = catalog
        self.journal = journal
        self.supervisor = supervisor
        self.execd = execd
        self.model_catalog = model_catalog
        self._file_locks: dict[str, asyncio.Lock] = {}
        self._file_locks_guard = asyncio.Lock()

    def require_allowed_model(self, model: str) -> None:
        if model not in self.settings.allowed_models():
            raise ApiProblem(
                422,
                "model_not_allowed",
                "only configured LiteLLM model aliases may be used",
            )

    async def require_session(self, session_id: str) -> SessionRecord:
        record = await self.catalog.get(session_id)
        if record is None:
            raise ApiProblem(404, "session_not_found", "session does not exist")
        return record

    async def execd_request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: Any | None = None,
        headers: dict[str, str] | None = None,
        files: Any | None = None,
    ) -> Response:
        try:
            upstream = await self.execd.request(
                method, path, params=params, json=json_body, headers=headers, files=files
            )
        except ExecdError as exc:
            raise ApiProblem(exc.status_code, "execd_request_failed", exc.detail) from exc
        response_headers = {
            name: value
            for name in ("content-disposition", "content-range", "execd-commands-tail-cursor")
            if (value := upstream.headers.get(name)) is not None
        }
        return Response(
            content=upstream.content,
            status_code=upstream.status_code,
            media_type=upstream.headers.get("content-type"),
            headers=response_headers,
        )

    async def get_file_info(self, path: str) -> dict[str, Any]:
        try:
            info_response = await self.execd.request("GET", "/files/info", params={"path": path})
            info = info_response.json()
            entry = info.get(path) if isinstance(info, dict) else None
        except ExecdError as exc:
            raise ApiProblem(exc.status_code, "execd_request_failed", exc.detail) from exc
        if not isinstance(entry, dict):
            raise ApiProblem(404, "file_not_found", "path does not identify a file system entry")
        return entry

    async def get_file_bytes(self, path: str) -> tuple[dict[str, Any], bytes]:
        entry = await self.get_file_info(path)
        if entry.get("type") != "file":
            raise ApiProblem(422, "not_a_file", "path must identify an existing regular file")
        try:
            content_response = await self.execd.request(
                "GET", "/files/download", params={"path": path}
            )
        except ExecdError as exc:
            raise ApiProblem(exc.status_code, "execd_request_failed", exc.detail) from exc
        return entry, content_response.content

    async def get_editable_text_file(self, path: str) -> tuple[dict[str, Any], bytes]:
        entry, content = await self.get_file_bytes(path)
        size = entry.get("size")
        if not isinstance(size, int) or size > self.max_text_file_bytes:
            raise ApiProblem(
                422,
                "not_editable_text_file",
                f"file must be at most {self.max_text_file_bytes} bytes for text editing",
            )
        try:
            decoded = content.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ApiProblem(422, "not_editable_text_file", "file is not valid UTF-8 text") from exc
        if "\x00" in decoded:
            raise ApiProblem(422, "not_editable_text_file", "file contains NUL bytes")
        return entry, content

    @staticmethod
    def file_etag(content: bytes) -> str:
        return f'"sha256:{hashlib.sha256(content).hexdigest()}"'

    @staticmethod
    def require_if_match(if_match: str | None, current_etag: str, entry: dict[str, Any]) -> None:
        if if_match is None:
            raise ApiProblem(
                428, "precondition_required", "If-Match with the file ETag is required"
            )
        if if_match != current_etag:
            raise ApiProblem(
                412,
                "file_conflict",
                "file changed after it was read",
                extra={
                    "current_etag": current_etag,
                    "size": entry.get("size"),
                    "modified_at": entry.get("modified_at"),
                },
            )

    async def file_lock(self, path: str) -> asyncio.Lock:
        async with self._file_locks_guard:
            return self._file_locks.setdefault(path, asyncio.Lock())

    async def upload_bytes(
        self,
        path: str,
        content: bytes,
        *,
        filename: str,
        content_type: str,
        source_entry: dict[str, Any] | None = None,
    ) -> None:
        metadata: dict[str, Any] = {"path": path}
        if source_entry is not None:
            for key in ("owner", "group", "mode"):
                value = source_entry.get(key)
                if value is not None:
                    metadata[key] = value
        files = {
            "metadata": (
                "metadata.json",
                json.dumps(metadata, separators=(",", ":")),
                "application/json",
            ),
            "file": (filename, content, content_type),
        }
        await self.execd_request("POST", "/files/upload", files=files)

    @property
    def session_root(self) -> Path:
        return self.settings.pi_session_dir
