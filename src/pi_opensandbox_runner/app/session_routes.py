from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Query, Response

from ..rpc import RpcError, RpcProcessExited, SessionCapacityExceeded
from ..schemas import (
    PromptAccepted,
    PromptCreate,
    SessionCreate,
    SessionOut,
    SessionPage,
    SessionPatch,
)
from .context import BridgeContext
from .problems import ApiProblem
from .session_state import decode_cursor, encode_cursor, session_out


def register_session_routes(router: APIRouter, ctx: BridgeContext) -> None:
    @router.post("/sessions", response_model=SessionOut, status_code=201)
    async def create_session(payload: SessionCreate) -> SessionOut:
        provider = payload.provider or ctx.settings.default_provider
        model = payload.model or ctx.settings.default_model
        if not provider or not model:
            raise ApiProblem(
                422,
                "model_required",
                "provider and model are required when no container defaults are configured",
            )
        session_id = str(uuid.uuid4())
        cwd = (
            Path(payload.cwd)
            if payload.cwd is not None
            else ctx.settings.workspace_root / session_id
        )
        if not cwd.is_absolute():
            raise ApiProblem(422, "invalid_cwd", "cwd must be an absolute container path")
        try:
            cwd.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise ApiProblem(422, "invalid_cwd", f"cannot create cwd: {exc}") from exc
        record = await ctx.catalog.create(
            session_id=session_id,
            name=payload.name,
            cwd=str(cwd.resolve()),
            provider=provider,
            model=model,
            thinking_level=payload.thinking_level,
        )
        return await session_out(record, ctx.supervisor)

    @router.get("/sessions", response_model=SessionPage)
    async def list_sessions(
        cursor: str | None = None,
        limit: Annotated[int, Query(ge=1, le=200)] = 50,
    ) -> SessionPage:
        before_time: str | None = None
        before_id: str | None = None
        if cursor:
            try:
                decoded = json.loads(decode_cursor(cursor))
                before_time = str(decoded["updated_at"])
                before_id = str(decoded["id"])
            except (ValueError, KeyError, json.JSONDecodeError) as exc:
                raise ApiProblem(422, "invalid_cursor", "session cursor is invalid") from exc
        records = await ctx.catalog.list_page(
            limit=limit + 1,
            before_updated_at=before_time,
            before_id=before_id,
        )
        has_more = len(records) > limit
        page_records = records[:limit]
        items = [await session_out(record, ctx.supervisor) for record in page_records]
        next_cursor = None
        if has_more and page_records:
            last = page_records[-1]
            next_cursor = encode_cursor({"updated_at": last.updated_at, "id": last.id})
        return SessionPage(items=items, next_cursor=next_cursor, has_more=has_more)

    @router.get("/sessions/{session_id}", response_model=SessionOut)
    async def get_session(session_id: str) -> SessionOut:
        return await session_out(await ctx.require_session(session_id), ctx.supervisor)

    @router.patch("/sessions/{session_id}", response_model=SessionOut)
    async def patch_session(session_id: str, payload: SessionPatch) -> SessionOut:
        record = await ctx.require_session(session_id)
        process = ctx.supervisor.active(session_id)
        if process is not None:
            try:
                await process.request({"type": "set_session_name", "name": payload.name})
            except (RpcError, TimeoutError) as exc:
                raise ApiProblem(503, "pi_unavailable", str(exc)) from exc
        updated = await ctx.catalog.update_name(record.id, payload.name)
        assert updated is not None
        return await session_out(updated, ctx.supervisor)

    @router.delete("/sessions/{session_id}", status_code=204)
    async def delete_session(session_id: str, force: bool = False) -> Response:
        record = await ctx.require_session(session_id)
        if ctx.supervisor.active(session_id) is not None and not force:
            raise ApiProblem(409, "session_active", "stop the session or use force=true")
        if ctx.supervisor.active(session_id) is not None:
            await ctx.supervisor.stop(session_id, abort=True)
        deleted = await ctx.catalog.delete(session_id)
        assert deleted is not None
        if record.session_file:
            path = Path(record.session_file)
            try:
                if path.suffix == ".jsonl" and path.resolve().parent == ctx.session_root.resolve():
                    path.unlink(missing_ok=True)
            except OSError as exc:
                raise ApiProblem(500, "session_delete_failed", str(exc)) from exc
        await ctx.journal.delete(session_id)
        return Response(status_code=204)

    @router.post(
        "/sessions/{session_id}/prompts",
        response_model=PromptAccepted,
        status_code=202,
    )
    async def prompt(session_id: str, payload: PromptCreate) -> PromptAccepted:
        record = await ctx.require_session(session_id)
        command_id = str(uuid.uuid4())
        async with ctx.supervisor.command_lock(session_id):
            try:
                process = await ctx.supervisor.get_or_start(record)
                state_response = await process.request({"type": "get_state"})
                state_data = state_response.get("data")
                streaming = bool(isinstance(state_data, dict) and state_data.get("isStreaming"))
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
                    await ctx.catalog.update_model_settings(
                        session_id, provider=payload.provider, model=payload.model
                    )
                if payload.thinking_level is not None:
                    await process.request(
                        {"type": "set_thinking_level", "level": payload.thinking_level}
                    )
                    await ctx.catalog.update_model_settings(
                        session_id,
                        thinking_level=payload.thinking_level,
                        update_thinking_level=True,
                    )
                command_type = "follow_up" if delivery == "follow_up" else delivery
                await process.request(
                    {"id": command_id, "type": command_type, "message": payload.message}
                )
            except ApiProblem:
                raise
            except SessionCapacityExceeded as exc:
                raise ApiProblem(429, "session_capacity_exceeded", str(exc)) from exc
            except (RpcError, RpcProcessExited, TimeoutError, OSError) as exc:
                raise ApiProblem(503, "pi_unavailable", str(exc)) from exc
        await ctx.journal.append(
            session_id,
            "bridge",
            {"type": "input_accepted", "command_id": command_id, "delivery": delivery},
        )
        return PromptAccepted(command_id=command_id, session_id=session_id, delivery=delivery)
