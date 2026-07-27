from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from typing import Annotated, Any

from fastapi import (
    APIRouter,
    Depends,
    FastAPI,
    File,
    Form,
    Header,
    Query,
    Request,
    Response,
    UploadFile,
    status,
)
from fastapi.exceptions import RequestValidationError
from fastapi.openapi.docs import get_swagger_ui_html
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from .catalog import Catalog, SessionRecord
from .config import Settings
from .execd import ExecdClient, ExecdError
from .journal import EventCursorExpired, EventJournal
from .rpc import (
    PiRpcProcess,
    RpcError,
    RpcProcessExited,
    SessionCapacityExceeded,
    SessionSupervisor,
)
from .schemas import (
    CommandCreate,
    EntryPage,
    PromptAccepted,
    PromptCreate,
    SessionCreate,
    SessionOut,
    SessionPage,
    SessionPatch,
)

BEARER_AUTH = HTTPBearer(auto_error=False)
BearerCredentials = Annotated[HTTPAuthorizationCredentials | None, Depends(BEARER_AUTH)]


class ApiProblem(Exception):
    def __init__(
        self,
        status_code: int,
        code: str,
        detail: str,
        *,
        extra: dict[str, Any] | None = None,
    ):
        self.status_code = status_code
        self.code = code
        self.detail = detail
        self.extra = extra or {}


def _problem_response(problem: ApiProblem, request: Request) -> JSONResponse:
    content: dict[str, Any] = {
        "type": f"https://pi-runner.local/problems/{problem.code}",
        "title": problem.code.replace("_", " "),
        "status": problem.status_code,
        "detail": problem.detail,
        "instance": str(request.url.path),
        "code": problem.code,
    }
    content.update(problem.extra)
    return JSONResponse(
        status_code=problem.status_code,
        content=content,
        media_type="application/problem+json",
    )


def create_app(settings: Settings | None = None) -> FastAPI:
    resolved = settings or Settings.from_env()
    catalog = Catalog(resolved.state_root / "bridge.db", resolved.pi_session_dir)
    journal = EventJournal(
        resolved.state_root / "events",
        catalog,
        segment_bytes=resolved.event_segment_bytes,
        segment_count=resolved.event_segment_count,
    )
    supervisor = SessionSupervisor(resolved, catalog, journal)
    execd = ExecdClient(resolved.execd_url)
    max_text_file_bytes = 1_048_576
    file_locks: dict[str, asyncio.Lock] = {}
    file_locks_guard = asyncio.Lock()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        resolved.workspace_root.mkdir(parents=True, exist_ok=True)
        await catalog.initialize()
        await supervisor.start()
        app.state.ready = True
        try:
            yield
        finally:
            app.state.ready = False
            await supervisor.close()
            await execd.close()

    app = FastAPI(
        title="Pi OpenSandbox Runner",
        version="0.1.0",
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
    )
    app.state.settings = resolved
    app.state.catalog = catalog
    app.state.journal = journal
    app.state.supervisor = supervisor
    app.state.execd = execd
    app.state.ready = False

    # OpenSandbox may expose the bridge through either its direct ingress or
    # its localhost-only server proxy.  The latter includes the sandbox ID in
    # the URL, which is unknown until after creation.  Relative URLs keep the
    # Swagger document and API calls below whichever proxy prefix was used.
    original_openapi = app.openapi

    def openapi() -> dict[str, Any]:
        schema = original_openapi()
        schema["servers"] = [{"url": "."}]
        return schema

    app.openapi = openapi  # type: ignore[method-assign]

    @app.get("/docs", include_in_schema=False)
    async def docs() -> HTMLResponse:
        return get_swagger_ui_html(
            openapi_url="openapi.json",
            title=f"{app.title} - Swagger UI",
        )

    @app.exception_handler(ApiProblem)
    async def handle_problem(request: Request, exc: ApiProblem) -> JSONResponse:
        return _problem_response(exc, request)

    @app.exception_handler(RequestValidationError)
    async def handle_validation(request: Request, exc: RequestValidationError) -> JSONResponse:
        return _problem_response(
            ApiProblem(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                "validation_error",
                "request validation failed",
                extra={
                    "errors": [
                        {
                            key: str(value) if key == "ctx" else value
                            for key, value in error.items()
                        }
                        for error in exc.errors()
                    ]
                },
            ),
            request,
        )

    @app.get("/healthz")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/readyz")
    async def ready(request: Request) -> dict[str, str]:
        if not request.app.state.ready:
            raise ApiProblem(503, "not_ready", "bridge is not ready")
        return {"status": "ready"}

    def authenticate(
        credentials: BearerCredentials = None,
    ) -> None:
        if (
            credentials is None
            or credentials.scheme.lower() != "bearer"
            or not hmac.compare_digest(credentials.credentials, resolved.api_token)
        ):
            raise ApiProblem(401, "unauthorized", "a valid bearer token is required")

    router = APIRouter(prefix="/v1", dependencies=[Depends(authenticate)])

    async def require_session(session_id: str) -> SessionRecord:
        record = await catalog.get(session_id)
        if record is None:
            raise ApiProblem(404, "session_not_found", "session does not exist")
        return record

    async def execd_request(
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: Any | None = None,
        headers: dict[str, str] | None = None,
        files: Any | None = None,
    ) -> Response:
        try:
            upstream = await execd.request(
                method, path, params=params, json=json, headers=headers, files=files
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

    async def get_file_info(path: str) -> dict[str, Any]:
        try:
            info_response = await execd.request("GET", "/files/info", params={"path": path})
            info = info_response.json()
            entry = info.get(path) if isinstance(info, dict) else None
        except ExecdError as exc:
            raise ApiProblem(exc.status_code, "execd_request_failed", exc.detail) from exc
        if not isinstance(entry, dict):
            raise ApiProblem(404, "file_not_found", "path does not identify a file system entry")
        return entry

    async def get_file_bytes(path: str) -> tuple[dict[str, Any], bytes]:
        entry = await get_file_info(path)
        if entry.get("type") != "file":
            raise ApiProblem(422, "not_a_file", "path must identify an existing regular file")
        try:
            content_response = await execd.request("GET", "/files/download", params={"path": path})
        except ExecdError as exc:
            raise ApiProblem(exc.status_code, "execd_request_failed", exc.detail) from exc
        return entry, content_response.content

    async def get_editable_text_file(path: str) -> tuple[dict[str, Any], bytes]:
        """Read an existing, reasonably sized UTF-8 text file for conditional saving."""
        entry, content = await get_file_bytes(path)
        size = entry.get("size")
        if not isinstance(size, int) or size > max_text_file_bytes:
            raise ApiProblem(
                422,
                "not_editable_text_file",
                f"file must be at most {max_text_file_bytes} bytes for text editing",
            )
        try:
            decoded = content.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ApiProblem(422, "not_editable_text_file", "file is not valid UTF-8 text") from exc
        if "\x00" in decoded:
            raise ApiProblem(422, "not_editable_text_file", "file contains NUL bytes")
        return entry, content

    def file_etag(content: bytes) -> str:
        return f'"sha256:{hashlib.sha256(content).hexdigest()}"'

    def require_if_match(if_match: str | None, current_etag: str, entry: dict[str, Any]) -> None:
        if if_match is None:
            raise ApiProblem(
                428,
                "precondition_required",
                "If-Match with the file ETag is required",
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

    async def file_lock(path: str) -> asyncio.Lock:
        async with file_locks_guard:
            return file_locks.setdefault(path, asyncio.Lock())

    async def upload_bytes(
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
        await execd_request("POST", "/files/upload", files=files)

    @router.post("/sessions", response_model=SessionOut, status_code=201)
    async def create_session(payload: SessionCreate) -> SessionOut:
        provider = payload.provider or resolved.default_provider
        model = payload.model or resolved.default_model
        if not provider or not model:
            raise ApiProblem(
                422,
                "model_required",
                "provider and model are required when no container defaults are configured",
            )
        session_id = str(uuid.uuid4())
        cwd = Path(payload.cwd) if payload.cwd is not None else resolved.workspace_root / session_id
        if not cwd.is_absolute():
            raise ApiProblem(422, "invalid_cwd", "cwd must be an absolute container path")
        try:
            cwd.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise ApiProblem(422, "invalid_cwd", f"cannot create cwd: {exc}") from exc
        record = await catalog.create(
            session_id=session_id,
            name=payload.name,
            cwd=str(cwd.resolve()),
            provider=provider,
            model=model,
            thinking_level=payload.thinking_level,
        )
        return await _session_out(record, supervisor)

    @router.get("/sessions", response_model=SessionPage)
    async def list_sessions(
        cursor: str | None = None,
        limit: Annotated[int, Query(ge=1, le=200)] = 50,
    ) -> SessionPage:
        before_time: str | None = None
        before_id: str | None = None
        if cursor:
            try:
                decoded = json.loads(_b64decode(cursor))
                before_time = str(decoded["updated_at"])
                before_id = str(decoded["id"])
            except (ValueError, KeyError, json.JSONDecodeError) as exc:
                raise ApiProblem(422, "invalid_cursor", "session cursor is invalid") from exc
        records = await catalog.list_page(
            limit=limit + 1,
            before_updated_at=before_time,
            before_id=before_id,
        )
        has_more = len(records) > limit
        page_records = records[:limit]
        items = [await _session_out(record, supervisor) for record in page_records]
        next_cursor = None
        if has_more and page_records:
            last = page_records[-1]
            next_cursor = _b64encode({"updated_at": last.updated_at, "id": last.id})
        return SessionPage(items=items, next_cursor=next_cursor, has_more=has_more)

    @router.get("/sessions/{session_id}", response_model=SessionOut)
    async def get_session(session_id: str) -> SessionOut:
        return await _session_out(await require_session(session_id), supervisor)

    @router.patch("/sessions/{session_id}", response_model=SessionOut)
    async def patch_session(session_id: str, payload: SessionPatch) -> SessionOut:
        record = await require_session(session_id)
        process = supervisor.active(session_id)
        if process is not None:
            try:
                await process.request({"type": "set_session_name", "name": payload.name})
            except (RpcError, TimeoutError) as exc:
                raise ApiProblem(503, "pi_unavailable", str(exc)) from exc
        updated = await catalog.update_name(record.id, payload.name)
        assert updated is not None
        return await _session_out(updated, supervisor)

    @router.delete("/sessions/{session_id}", status_code=204)
    async def delete_session(session_id: str, force: bool = False) -> Response:
        record = await require_session(session_id)
        if supervisor.active(session_id) is not None and not force:
            raise ApiProblem(409, "session_active", "stop the session or use force=true")
        if supervisor.active(session_id) is not None:
            await supervisor.stop(session_id, abort=True)
        deleted = await catalog.delete(session_id)
        assert deleted is not None
        if record.session_file:
            path = Path(record.session_file)
            try:
                if (
                    path.suffix == ".jsonl"
                    and path.resolve().parent == resolved.pi_session_dir.resolve()
                ):
                    path.unlink(missing_ok=True)
            except OSError as exc:
                raise ApiProblem(500, "session_delete_failed", str(exc)) from exc
        await journal.delete(session_id)
        return Response(status_code=204)

    @router.post(
        "/sessions/{session_id}/prompts",
        response_model=PromptAccepted,
        status_code=202,
    )
    async def prompt(session_id: str, payload: PromptCreate) -> PromptAccepted:
        record = await require_session(session_id)
        command_id = str(uuid.uuid4())
        async with supervisor.command_lock(session_id):
            try:
                process = await supervisor.get_or_start(record)
                state_response = await process.request({"type": "get_state"})
                state_data = state_response.get("data")
                streaming = bool(
                    isinstance(state_data, dict) and state_data.get("isStreaming")
                )
                if payload.delivery == "auto":
                    delivery = "follow_up" if streaming else "prompt"
                else:
                    delivery = payload.delivery
                    if not streaming:
                        raise ApiProblem(
                            409,
                            "session_not_streaming",
                            f"{delivery} requires a running agent turn",
                        )
                if payload.provider is not None and payload.model is not None:
                    await process.request(
                        {
                            "type": "set_model",
                            "provider": payload.provider,
                            "modelId": payload.model,
                        }
                    )
                    await catalog.update_model_settings(
                        session_id,
                        provider=payload.provider,
                        model=payload.model,
                    )
                if payload.thinking_level is not None:
                    await process.request(
                        {"type": "set_thinking_level", "level": payload.thinking_level}
                    )
                    await catalog.update_model_settings(
                        session_id,
                        thinking_level=payload.thinking_level,
                        update_thinking_level=True,
                    )
                command_type = "follow_up" if delivery == "follow_up" else delivery
                await process.request(
                    {
                        "id": command_id,
                        "type": command_type,
                        "message": payload.message,
                    }
                )
            except ApiProblem:
                raise
            except SessionCapacityExceeded as exc:
                raise ApiProblem(429, "session_capacity_exceeded", str(exc)) from exc
            except (RpcError, RpcProcessExited, TimeoutError, OSError) as exc:
                raise ApiProblem(503, "pi_unavailable", str(exc)) from exc
        await journal.append(
            session_id,
            "bridge",
            {
                "type": "input_accepted",
                "command_id": command_id,
                "delivery": delivery,
            },
        )
        return PromptAccepted(
            command_id=command_id,
            session_id=session_id,
            delivery=delivery,
        )

    @router.get("/files")
    async def list_files(
        path: str = "/root/workspace",
        depth: Annotated[int, Query(ge=0, le=64)] = 1,
    ) -> Response:
        """List a directory, like ``ls``; depth 1 returns immediate children."""
        return await execd_request(
            "GET", "/directories/list", params={"path": path, "depth": depth}
        )

    @router.get("/files/content")
    async def read_file(
        path: str,
        offset: Annotated[int | None, Query(ge=1)] = None,
        limit: Annotated[int | None, Query(ge=1, le=100_000)] = None,
        range_header: Annotated[str | None, Header(alias="Range")] = None,
    ) -> Response:
        """Read or download a file; offset/limit select a range of text lines."""
        if range_header is not None and (offset is not None or limit is not None):
            raise ApiProblem(
                422,
                "invalid_file_range",
                "Range cannot be combined with offset or limit",
            )
        params: dict[str, Any] = {"path": path}
        if offset is not None:
            params["offset"] = offset
        if limit is not None:
            params["limit"] = limit
        headers = {"Range": range_header} if range_header is not None else None
        try:
            upstream = await execd.request("GET", "/files/download", params=params, headers=headers)
        except ExecdError as exc:
            raise ApiProblem(exc.status_code, "execd_request_failed", exc.detail) from exc
        response_headers = {
            name: value
            for name in ("content-disposition", "content-range")
            if (value := upstream.headers.get(name)) is not None
        }
        if offset is None and limit is None and range_header is None:
            entry = await get_file_info(path)
            response_headers["ETag"] = file_etag(upstream.content)
            response_headers["X-File-Size"] = str(entry.get("size", ""))
            if (modified_at := entry.get("modified_at")) is not None:
                response_headers["X-File-Modified-At"] = str(modified_at)
        return Response(
            content=upstream.content,
            status_code=upstream.status_code,
            media_type=upstream.headers.get("content-type"),
            headers=response_headers,
        )

    @router.delete("/files", status_code=204)
    async def delete_file(
        path: str,
        if_match: Annotated[str | None, Header(alias="If-Match")] = None,
    ) -> Response:
        """Delete one file; directory deletion is intentionally not exposed here."""
        lock = await file_lock(path)
        async with lock:
            entry, content = await get_file_bytes(path)
            require_if_match(if_match, file_etag(content), entry)
            await execd_request("DELETE", "/files", params={"path": path})
        return Response(status_code=204)

    @router.put("/files/content", status_code=204)
    async def update_text_file(
        path: str,
        request: Request,
        if_match: Annotated[str | None, Header(alias="If-Match")] = None,
    ) -> Response:
        """Replace an existing UTF-8 text file in full; binary files are rejected."""
        content_type = request.headers.get("content-type", "")
        if not content_type.lower().startswith("text/plain"):
            raise ApiProblem(
                415,
                "unsupported_media_type",
                "Content-Type must be text/plain; charset=utf-8",
            )
        encoded_content = await request.body()
        if len(encoded_content) > max_text_file_bytes:
            raise ApiProblem(
                422,
                "not_editable_text_file",
                f"replacement content must be at most {max_text_file_bytes} bytes",
            )
        try:
            decoded_content = encoded_content.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ApiProblem(
                422, "not_editable_text_file", "replacement is not valid UTF-8 text"
            ) from exc
        if "\x00" in decoded_content:
            raise ApiProblem(422, "not_editable_text_file", "replacement contains NUL bytes")
        lock = await file_lock(path)
        async with lock:
            entry, current_content = await get_editable_text_file(path)
            require_if_match(if_match, file_etag(current_content), entry)
            temporary_path = f"{path}.pi-runner-{uuid.uuid4().hex}.tmp"
            try:
                await upload_bytes(
                    temporary_path,
                    encoded_content,
                    filename=Path(path).name or "file",
                    content_type="text/plain; charset=utf-8",
                    source_entry=entry,
                )
                await execd_request(
                    "POST",
                    "/command",
                    json={
                        "command": (
                            "python -c 'import os; "
                            "os.replace(os.environ[\"PI_RUNNER_TMP\"], "
                            "os.environ[\"PI_RUNNER_DEST\"])'"
                        ),
                        "envs": {
                            "PI_RUNNER_TMP": temporary_path,
                            "PI_RUNNER_DEST": path,
                        },
                        "timeout": 10_000,
                    },
                )
                _, written_content = await get_file_bytes(path)
                if file_etag(written_content) != file_etag(encoded_content):
                    raise ApiProblem(
                        502,
                        "atomic_replace_failed",
                        "replacement command completed without writing the expected content",
                    )
            except BaseException:
                with suppress(ApiProblem):
                    await execd_request("DELETE", "/files", params={"path": temporary_path})
                raise
        return Response(status_code=204, headers={"ETag": file_etag(encoded_content)})

    @router.post("/files/upload", status_code=201)
    async def upload_file(
        path: Annotated[str, Form(min_length=1, max_length=4096)],
        file: Annotated[UploadFile, File()],
        if_none_match: Annotated[str | None, Header(alias="If-None-Match")] = None,
    ) -> Response:
        """Upload one file to a path, or use its original name in a target directory."""
        if if_none_match != "*":
            raise ApiProblem(
                428,
                "precondition_required",
                "If-None-Match: * is required when creating a file",
            )
        destination = path
        if path.endswith("/"):
            destination = str(Path(path) / Path(file.filename or "upload").name)
        else:
            try:
                info_response = await execd.request("GET", "/files/info", params={"path": path})
            except ExecdError as exc:
                if exc.status_code != 404:
                    raise ApiProblem(exc.status_code, "execd_request_failed", exc.detail) from exc
            else:
                info = info_response.json()
                entry = info.get(path) if isinstance(info, dict) else None
                if isinstance(entry, dict) and entry.get("type") == "directory":
                    destination = str(Path(path) / Path(file.filename or "upload").name)
        lock = await file_lock(destination)
        async with lock:
            try:
                await get_file_info(destination)
            except ApiProblem as exc:
                if exc.status_code != 404:
                    raise
            else:
                raise ApiProblem(412, "file_exists", "a file already exists at the destination")
            content = await file.read()
            await upload_bytes(
                destination,
                content,
                filename=file.filename or "upload",
                content_type=file.content_type or "application/octet-stream",
            )
        return Response(status_code=201, headers={"ETag": file_etag(content)})

    @router.post("/commands")
    async def run_command(payload: CommandCreate) -> Response:
        """Execute a shell command through Execd and return its SSE output."""
        try:
            upstream = await execd.stream(
                "POST", "/command", json=payload.model_dump(exclude_none=True)
            )
        except ExecdError as exc:
            raise ApiProblem(exc.status_code, "execd_request_failed", exc.detail) from exc

        async def body() -> AsyncIterator[bytes]:
            try:
                async for chunk in upstream.aiter_bytes():
                    yield chunk
            finally:
                await upstream.aclose()

        return StreamingResponse(
            body(),
            status_code=upstream.status_code,
            media_type=upstream.headers.get("content-type", "text/event-stream"),
        )

    @router.delete("/commands/{command_id}", status_code=202)
    async def abort_command(command_id: str) -> Response:
        return await execd_request("DELETE", "/command", params={"id": command_id})

    @router.get("/commands/{command_id}")
    async def command_status(command_id: str) -> Response:
        return await execd_request("GET", f"/command/status/{command_id}")

    @router.get("/commands/{command_id}/logs")
    async def command_logs(
        command_id: str,
        cursor: Annotated[int | None, Query(ge=0)] = None,
    ) -> Response:
        params = {"cursor": cursor} if cursor is not None else None
        return await execd_request("GET", f"/command/{command_id}/logs", params=params)

    @router.post("/sessions/{session_id}/abort", status_code=202)
    async def abort(session_id: str) -> dict[str, str]:
        await require_session(session_id)
        process = supervisor.active(session_id)
        if process is None:
            raise ApiProblem(409, "session_stopped", "session is not running")
        try:
            await process.request({"type": "abort"})
        except (RpcError, TimeoutError) as exc:
            raise ApiProblem(503, "pi_unavailable", str(exc)) from exc
        return {"status": "accepted"}

    @router.post("/sessions/{session_id}/stop", status_code=204)
    async def stop_session(session_id: str) -> Response:
        await require_session(session_id)
        await supervisor.stop(session_id, abort=True)
        return Response(status_code=204)

    @router.get("/sessions/{session_id}/entries", response_model=EntryPage)
    async def entries(
        session_id: str,
        cursor: str | None = None,
        limit: Annotated[int, Query(ge=1, le=1000)] = 100,
    ) -> EntryPage:
        record = await require_session(session_id)
        try:
            all_entries, leaf_id = await _get_entries(record, supervisor, cursor)
        except RpcError as exc:
            if "Entry not found" in str(exc):
                raise ApiProblem(422, "invalid_cursor", str(exc)) from exc
            raise ApiProblem(503, "pi_unavailable", str(exc)) from exc
        has_more = len(all_entries) > limit
        page = all_entries[:limit]
        next_cursor = str(page[-1].get("id")) if page else cursor
        return EntryPage(
            items=page,
            next_cursor=next_cursor,
            has_more=has_more,
            leaf_id=leaf_id,
        )

    @router.get("/sessions/{session_id}/events")
    async def events(
        session_id: str,
        cursor: int | None = Query(default=None, ge=0),
        last_event_id: Annotated[str | None, Header(alias="Last-Event-ID")] = None,
    ) -> StreamingResponse:
        await require_session(session_id)
        start = cursor or 0
        if last_event_id is not None:
            try:
                start = int(last_event_id)
            except ValueError as exc:
                raise ApiProblem(422, "invalid_cursor", "Last-Event-ID must be an integer") from exc
        try:
            await journal.ensure_cursor(session_id, start)
        except EventCursorExpired as exc:
            raise ApiProblem(
                410,
                "event_cursor_expired",
                "event cursor is no longer retained",
                extra={"oldest_cursor": exc.oldest_cursor},
            ) from exc

        async def stream() -> AsyncIterator[str]:
            current = start
            while True:
                batch = await journal.read_after(session_id, current)
                if batch:
                    for item in batch:
                        current = int(item["seq"])
                        event = item.get("event")
                        event_name = (
                            str(event.get("type", "message"))
                            if isinstance(event, dict)
                            else "message"
                        )
                        data = json.dumps(item, ensure_ascii=False, separators=(",", ":"))
                        yield f"id: {current}\nevent: {event_name}\ndata: {data}\n\n"
                    continue
                yield ": heartbeat\n\n"
                await journal.wait_for_change(session_id, 15.0)

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
            },
        )

    app.include_router(router)
    return app


async def _get_entries(
    record: SessionRecord,
    supervisor: SessionSupervisor,
    cursor: str | None,
) -> tuple[list[dict[str, Any]], str | None]:
    process = supervisor.active(record.id)
    if process is not None:
        command: dict[str, Any] = {"type": "get_entries"}
        if cursor is not None:
            command["since"] = cursor
        response = await process.request(command)
        data = response.get("data")
        if not isinstance(data, dict):
            raise RpcError("Pi get_entries returned invalid data")
        entries = data.get("entries")
        return (
            [item for item in entries if isinstance(item, dict)]
            if isinstance(entries, list)
            else [],
            str(data["leafId"]) if data.get("leafId") is not None else None,
        )
    if not record.session_file or not Path(record.session_file).is_file():
        if cursor is not None:
            raise ApiProblem(422, "invalid_cursor", "entry cursor does not exist")
        return [], None
    stored = _read_session_entries(Path(record.session_file))
    leaf_id = str(stored[-1].get("id")) if stored else None
    if cursor is None:
        return stored, leaf_id
    for index, entry in enumerate(stored):
        if entry.get("id") == cursor:
            return stored[index + 1 :], leaf_id
    raise ApiProblem(422, "invalid_cursor", "entry cursor does not exist")


def _read_session_entries(path: Path) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    try:
        with path.open(encoding="utf-8") as stream:
            for line in stream:
                try:
                    item = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(item, dict) and item.get("type") != "session":
                    entries.append(item)
    except OSError:
        return []
    return entries


async def _session_out(
    record: SessionRecord,
    supervisor: SessionSupervisor,
) -> SessionOut:
    process: PiRpcProcess | None = supervisor.active(record.id)
    entries: list[dict[str, Any]]
    leaf_id: str | None
    try:
        entries, leaf_id = await _get_entries(record, supervisor, None)
    except (RpcError, ApiProblem):
        entries, leaf_id = [], None
    message_count = sum(1 for entry in entries if entry.get("type") == "message")
    materialized = bool(record.session_file and Path(record.session_file).is_file())
    state = (
        ("running" if process.is_streaming else "idle")
        if process is not None
        else record.last_status
    )
    return SessionOut(
        id=record.id,
        name=record.name,
        cwd=record.cwd,
        provider=record.provider,
        model=record.model,
        thinking_level=record.thinking_level,
        session_file=record.session_file,
        materialized=materialized,
        state=state,
        is_streaming=bool(process and process.is_streaming),
        message_count=message_count,
        leaf_id=leaf_id,
        created_at=record.created_at,
        updated_at=record.updated_at,
        last_error=record.last_error,
    )


def _b64encode(value: dict[str, str]) -> str:
    raw = json.dumps(value, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _b64decode(value: str) -> str:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(value + padding).decode()
