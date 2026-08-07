from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Annotated, Literal

from fastapi import APIRouter, Header, Query, Response

from ..rpc import RpcError, RpcProcessExited, SessionCapacityExceeded
from ..schemas import (
    ModelCatalogOut,
    PromptAccepted,
    PromptCreate,
    SessionCreate,
    SessionOut,
    SessionPage,
    SessionPatch,
    SystemPromptUpdate,
)
from .context import BridgeContext
from .problems import ApiProblem
from .session_state import decode_cursor, encode_cursor, session_out


def _accepted_delivery(value: object) -> Literal["prompt", "steer", "follow_up"]:
    if value == "prompt":
        return "prompt"
    if value == "steer":
        return "steer"
    if value == "follow_up":
        return "follow_up"
    raise ApiProblem(500, "invalid_event", "stored input_accepted delivery is invalid")


def register_session_routes(router: APIRouter, ctx: BridgeContext) -> None:
    @router.post(
        "/sessions",
        response_model=SessionOut,
        status_code=201,
        summary="创建逻辑 Session",
        description=(
            "只创建 bridge 元数据和工作目录，不会立即启动 Pi 或发送模型请求。"
            "未指定 cwd 时使用独立的 `/root/workspace/<session-id>`。"
        ),
    )
    async def create_session(payload: SessionCreate) -> SessionOut:
        model = payload.model or ctx.settings.default_model
        if not model:
            raise ApiProblem(
                422,
                "model_required",
                "model is required when no container default is configured",
            )
        ctx.require_allowed_model(model)
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
            model=model,
            thinking_level=(
                str(payload.thinking_level) if payload.thinking_level is not None else None
            ),
            system_prompt=payload.system_prompt,
            system_prompt_mode=payload.system_prompt_mode,
        )
        return await session_out(record, ctx.supervisor)

    @router.get("/models", response_model=ModelCatalogOut, summary="列出可用 LiteLLM 模型")
    async def models() -> ModelCatalogOut:
        return ModelCatalogOut(models=sorted(ctx.settings.allowed_models()))

    @router.get(
        "/sessions",
        response_model=SessionPage,
        summary="列出历史 Session",
        description="按更新时间倒序分页；响应会包含每个 Session 的完整 system prompt。",
    )
    async def list_sessions(
        cursor: Annotated[str | None, Query(description="上一页返回的 next_cursor。")] = None,
        limit: Annotated[int, Query(ge=1, le=200, description="每页数量，范围 1 至 200。")] = 50,
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

    @router.get("/sessions/{session_id}", response_model=SessionOut, summary="读取 Session")
    async def get_session(session_id: str) -> SessionOut:
        return await session_out(await ctx.require_session(session_id), ctx.supervisor)

    @router.patch(
        "/sessions/{session_id}",
        response_model=SessionOut,
        summary="重命名 Session",
        description="当前仅允许修改 name；运行中的 Pi 会同步更新显示名称。",
    )
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

    @router.put(
        "/sessions/{session_id}/system-prompt",
        response_model=SessionOut,
        summary="更新 Session system prompt",
        description=(
            "完整替换该 Session 的自定义配置。append 保留 Pi 默认 coding prompt；"
            "replace 完全替换它。配置在下一条 prompt 前生效：idle Pi 会重启，"
            "正在生成时返回 409 session_streaming。"
        ),
    )
    async def update_system_prompt(session_id: str, payload: SystemPromptUpdate) -> SessionOut:
        async with ctx.supervisor.command_lock(session_id):
            record = await ctx.require_session(session_id)
            process = ctx.supervisor.active(session_id)
            if process is not None and process.is_streaming:
                raise ApiProblem(
                    409,
                    "session_streaming",
                    "stop or wait for the current agent turn before changing the system prompt",
                )
            updated = await ctx.catalog.update_system_prompt(
                record.id,
                system_prompt=payload.system_prompt,
                system_prompt_mode=payload.system_prompt_mode,
            )
        assert updated is not None
        return await session_out(updated, ctx.supervisor)

    @router.delete(
        "/sessions/{session_id}/system-prompt",
        response_model=SessionOut,
        summary="清除 Session system prompt",
        description=("恢复 Pi 默认 prompt；运行中的生成任务不能修改，返回 409 session_streaming。"),
    )
    async def clear_system_prompt(session_id: str) -> SessionOut:
        async with ctx.supervisor.command_lock(session_id):
            record = await ctx.require_session(session_id)
            process = ctx.supervisor.active(session_id)
            if process is not None and process.is_streaming:
                raise ApiProblem(
                    409,
                    "session_streaming",
                    "stop or wait for the current agent turn before changing the system prompt",
                )
            updated = await ctx.catalog.update_system_prompt(record.id, system_prompt=None)
        assert updated is not None
        return await session_out(updated, ctx.supervisor)

    @router.delete(
        "/sessions/{session_id}",
        status_code=204,
        summary="删除 Session",
        description=(
            "活动 Session 默认返回 409；传 force=true 会先停止 Pi。"
            "不会递归删除 cwd，避免删除共享工作目录。"
        ),
    )
    async def delete_session(
        session_id: str,
        force: Annotated[
            bool, Query(description="true 时先中止并停止活动 Pi，再删除 Session。")
        ] = False,
    ) -> Response:
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
        summary="向 Pi 发送 prompt",
        description=(
            "必要时恢复已停止的 Session。delivery=auto 在 idle 时使用 prompt，"
            "在生成时使用 follow_up；显式 steer/follow_up 要求 Pi 正在生成。"
            "可同时切换 model 与 thinking_level，设置会持久化到 Session。"
        ),
    )
    async def prompt(
        session_id: str,
        payload: PromptCreate,
        idempotency_key: Annotated[
            str | None,
            Header(
                alias="Idempotency-Key",
                min_length=1,
                max_length=128,
                description="调用方稳定请求 ID；相同 Session 内重复提交只执行一次。",
            ),
        ] = None,
    ) -> PromptAccepted:
        request_id = idempotency_key or str(uuid.uuid4())
        async with ctx.supervisor.command_lock(session_id):
            accepted = await ctx.journal.find_input_accepted(session_id, request_id)
            if accepted is not None:
                return PromptAccepted(
                    command_id=str(accepted["command_id"]),
                    request_id=request_id,
                    session_id=session_id,
                    delivery=_accepted_delivery(accepted.get("delivery")),
                )
            command_id = request_id
            try:
                if payload.model is not None:
                    ctx.require_allowed_model(payload.model)
                record = await ctx.require_session(session_id)
                if payload.model is not None:
                    if record.model != payload.model:
                        updated = await ctx.catalog.update_model_settings(
                            session_id, model=payload.model
                        )
                        assert updated is not None
                        record = updated
                else:
                    ctx.require_allowed_model(record.model)
                if await ctx.supervisor.needs_configuration_restart(record):
                    current = ctx.supervisor.active(session_id)
                    if current is not None and current.is_streaming:
                        raise ApiProblem(
                            409,
                            "model_catalog_update_pending",
                            "wait for the current agent turn before applying the "
                            "model catalog update",
                        )
                    await ctx.supervisor.stop(session_id, abort=False)
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
                if payload.model is not None:
                    await process.request(
                        {
                            "type": "set_model",
                            "provider": "litellm",
                            "modelId": payload.model,
                        }
                    )
                if payload.thinking_level is not None:
                    await process.request(
                        {"type": "set_thinking_level", "level": payload.thinking_level}
                    )
                    await ctx.catalog.update_model_settings(
                        session_id,
                        thinking_level=str(payload.thinking_level),
                        update_thinking_level=True,
                    )
                command_type = "follow_up" if delivery == "follow_up" else delivery
                ctx.supervisor.set_active_request(session_id, request_id)
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
            {
                "type": "input_accepted",
                "command_id": command_id,
                "request_id": request_id,
                "delivery": delivery,
            },
        )
        return PromptAccepted(
            command_id=command_id,
            request_id=request_id,
            session_id=session_id,
            delivery=delivery,
        )
