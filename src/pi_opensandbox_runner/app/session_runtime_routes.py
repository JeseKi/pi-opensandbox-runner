from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import APIRouter, Header, Query, Response
from fastapi.responses import StreamingResponse

from ..journal import EventCursorExpired
from ..rpc import RpcError
from ..schemas import EntryPage
from .context import BridgeContext
from .problems import ApiProblem
from .session_state import get_entries


def register_session_runtime_routes(router: APIRouter, ctx: BridgeContext) -> None:
    @router.post("/sessions/{session_id}/abort", status_code=202)
    async def abort(session_id: str) -> dict[str, str]:
        await ctx.require_session(session_id)
        process = ctx.supervisor.active(session_id)
        if process is None:
            raise ApiProblem(409, "session_stopped", "session is not running")
        try:
            await process.request({"type": "abort"})
        except (RpcError, TimeoutError) as exc:
            raise ApiProblem(503, "pi_unavailable", str(exc)) from exc
        return {"status": "accepted"}

    @router.post("/sessions/{session_id}/stop", status_code=204)
    async def stop_session(session_id: str) -> Response:
        await ctx.require_session(session_id)
        await ctx.supervisor.stop(session_id, abort=True)
        return Response(status_code=204)

    @router.get("/sessions/{session_id}/entries", response_model=EntryPage)
    async def entries(
        session_id: str,
        cursor: str | None = None,
        limit: Annotated[int, Query(ge=1, le=1000)] = 100,
    ) -> EntryPage:
        record = await ctx.require_session(session_id)
        try:
            all_entries, leaf_id = await get_entries(record, ctx.supervisor, cursor)
        except RpcError as exc:
            if "Entry not found" in str(exc):
                raise ApiProblem(422, "invalid_cursor", str(exc)) from exc
            raise ApiProblem(503, "pi_unavailable", str(exc)) from exc
        has_more = len(all_entries) > limit
        page = all_entries[:limit]
        next_cursor = str(page[-1].get("id")) if page else cursor
        return EntryPage(items=page, next_cursor=next_cursor, has_more=has_more, leaf_id=leaf_id)

    @router.get("/sessions/{session_id}/events")
    async def events(
        session_id: str,
        cursor: int | None = Query(default=None, ge=0),
        last_event_id: Annotated[str | None, Header(alias="Last-Event-ID")] = None,
    ) -> StreamingResponse:
        await ctx.require_session(session_id)
        start = cursor or 0
        if last_event_id is not None:
            try:
                start = int(last_event_id)
            except ValueError as exc:
                raise ApiProblem(422, "invalid_cursor", "Last-Event-ID must be an integer") from exc
        try:
            await ctx.journal.ensure_cursor(session_id, start)
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
                batch = await ctx.journal.read_after(session_id, current)
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
                await ctx.journal.wait_for_change(session_id, 15.0)

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )
