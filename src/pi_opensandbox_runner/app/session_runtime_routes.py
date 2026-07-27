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
    @router.post(
        "/sessions/{session_id}/abort",
        status_code=202,
        summary="中止当前 Pi turn",
        description="仅能中止正在运行的 Pi；已停止的 Session 返回 409 session_stopped。",
    )
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

    @router.post(
        "/sessions/{session_id}/stop",
        status_code=204,
        summary="停止 Pi RPC 进程",
        description="停止不删除 Session 元数据、Pi JSONL 历史或工作目录；后续 prompt 会自动恢复。",
    )
    async def stop_session(session_id: str) -> Response:
        await ctx.require_session(session_id)
        await ctx.supervisor.stop(session_id, abort=True)
        return Response(status_code=204)

    @router.get(
        "/sessions/{session_id}/entries",
        response_model=EntryPage,
        summary="按 entry cursor 获取对话上下文",
        description=(
            "cursor 是 Pi JSONL entry id，不是字符 offset 或 token 数。"
            "运行中通过 Pi RPC 读取，停止后直接读取持久化 JSONL。"
        ),
    )
    async def entries(
        session_id: str,
        cursor: Annotated[
            str | None, Query(description="上一批返回的 entry id；省略时从历史开始读取。")
        ] = None,
        limit: Annotated[
            int, Query(ge=1, le=1000, description="每批 entry 数量，范围 1 至 1000。")
        ] = 100,
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

    @router.get(
        "/sessions/{session_id}/events",
        summary="订阅 Session 事件",
        description=(
            "返回 text/event-stream。cursor 或 Last-Event-ID 是 bridge 的递增事件序号；"
            "超过保留范围时返回 410 event_cursor_expired。"
        ),
    )
    async def events(
        session_id: str,
        cursor: int | None = Query(
            default=None, ge=0, description="从该 bridge 事件序号之后开始推送。"
        ),
        last_event_id: Annotated[
            str | None,
            Header(alias="Last-Event-ID", description="SSE 断线重连时的最后事件序号。"),
        ] = None,
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
