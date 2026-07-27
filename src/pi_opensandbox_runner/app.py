from __future__ import annotations

import base64
import hmac
import json
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, Depends, FastAPI, Header, Query, Request, Response, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, StreamingResponse

from .catalog import Catalog, SessionRecord
from .config import Settings
from .journal import EventCursorExpired, EventJournal
from .rpc import (
    PiRpcProcess,
    RpcError,
    RpcProcessExited,
    SessionCapacityExceeded,
    SessionSupervisor,
)
from .schemas import (
    EntryPage,
    PromptAccepted,
    PromptCreate,
    SessionCreate,
    SessionOut,
    SessionPage,
    SessionPatch,
)


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

    app = FastAPI(
        title="Pi OpenSandbox Runner",
        version="0.1.0",
        lifespan=lifespan,
    )
    app.state.settings = resolved
    app.state.catalog = catalog
    app.state.journal = journal
    app.state.supervisor = supervisor
    app.state.ready = False

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
        authorization: Annotated[str | None, Header()] = None,
    ) -> None:
        expected = f"Bearer {resolved.api_token}"
        if authorization is None or not hmac.compare_digest(authorization, expected):
            raise ApiProblem(401, "unauthorized", "a valid bearer token is required")

    router = APIRouter(prefix="/v1", dependencies=[Depends(authenticate)])

    async def require_session(session_id: str) -> SessionRecord:
        record = await catalog.get(session_id)
        if record is None:
            raise ApiProblem(404, "session_not_found", "session does not exist")
        return record

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
