from __future__ import annotations

import asyncio
from typing import Any, Literal

from fastapi import Depends, FastAPI, Query, Response
from sqlalchemy import and_, desc, or_, select

from ..clients import BridgeClient, UpstreamProblem, as_manager_problem
from ..config import ManagerSettings
from ..crypto import CredentialCipher
from ..database import ManagerDatabase
from ..models import SessionBinding, TurnBinding
from ..openapi_docs import api_doc
from ..schemas import SessionEnsure, SessionOut, SessionPage
from ..security import Principal
from .helpers import (
    _find_bridge_session,
    _owned_instance,
    _prepare_session,
    _session_connection,
    _session_out,
)
from .pagination import _decode_page_cursor, _encode_page_cursor


def register_session_routes(
    app: FastAPI,
    db_control: ManagerDatabase,
    resolved: ManagerSettings,
    cipher: CredentialCipher,
    service_principal: Any,
) -> Any:

    @app.get(
        "/v1/instances/{subject_ref}/sessions",
        response_model=SessionPage,
        **api_doc(
            summary="分页列出 Instance 的 Session",
            description=(
                "列出当前 consumer 指定 Instance 中由 Manager 建立且仍保留绑定记录的 Session，"
                "用于管理界面、数据核对、灾难恢复和孤儿 Session 排查。\n\n"
                "结果来自 Manager 数据库快照，不会逐个请求 Pi Bridge，因此不会产生 N+1 上游调用；"
                "`state` 可能短暂滞后。需要准确状态时调用单 Session GET，该接口会刷新 Bridge 状态。"
                "已通过 DELETE 删除绑定的 Session 不会出现在列表中。\n\n"
                "结果固定按 `created_at DESC, id DESC` 排序。首次请求省略 cursor，后续把 "
                "`next_cursor` 原样传回。可以用 `q` 对 Session ID 和标题做不区分大小写的"
                "包含搜索，也可以按精确 `state`、`model_slug` 筛选；多个条件按 AND 组合，"
                "翻页期间应保持所有筛选条件不变。"
                "Instance 即使 stopped、destroyed 或 failed 仍可读取历史绑定快照。\n\n"
                "需要 service token 的 `sessions:read` scope；最大每页 500 条。"
            ),
            tag="Session（会话）",
            operation_id="list_manager_sessions",
            response_description="指定 Instance 的一页 Session 和下一页游标。",
        ),
    )
    async def list_sessions(
        subject_ref: str,
        cursor: str | None = Query(
            default=None,
            min_length=1,
            max_length=1024,
            description="上一页返回的 next_cursor；首次请求省略。",
        ),
        limit: int = Query(
            default=100,
            ge=1,
            le=500,
            description="每页条数，默认 100，最大 500。",
        ),
        state_filter: Literal["provisioning", "ready", "running", "failed"]
        | None = Query(
            default=None,
            alias="state",
            description="精确筛选 Manager 数据库中的 Session 状态。",
        ),
        model_slug: str | None = Query(
            default=None,
            min_length=1,
            max_length=120,
            description="精确筛选 Session 使用的 model slug。",
        ),
        q: str | None = Query(
            default=None,
            min_length=1,
            max_length=160,
            pattern=r".*\S.*",
            description="对 Session ID 和标题做不区分大小写的包含搜索。",
        ),
        caller: Principal = Depends(service_principal),
    ) -> SessionPage:
        caller.require("sessions:read")
        position = _decode_page_cursor(cursor, "session") if cursor else None
        with db_control.session() as db:
            instance = _owned_instance(db, caller, subject_ref)
            statement = select(SessionBinding).where(
                SessionBinding.instance_id == instance.id
            )
            if q is not None:
                search_query = q.strip()
                statement = statement.where(
                    or_(
                        SessionBinding.external_session_id.icontains(
                            search_query, autoescape=True
                        ),
                        SessionBinding.title.icontains(search_query, autoescape=True),
                    )
                )
            if state_filter is not None:
                statement = statement.where(SessionBinding.state == state_filter)
            if model_slug is not None:
                statement = statement.where(SessionBinding.model_slug == model_slug)
            if position is not None:
                statement = statement.where(
                    or_(
                        SessionBinding.created_at < position.created_at,
                        and_(
                            SessionBinding.created_at == position.created_at,
                            SessionBinding.external_session_id < position.item_id,
                        ),
                    )
                )
            records = list(
                db.scalars(
                    statement.order_by(
                        desc(SessionBinding.created_at),
                        desc(SessionBinding.external_session_id),
                    ).limit(limit + 1)
                )
            )
            has_more = len(records) > limit
            page_records = records[:limit]
            next_cursor = (
                _encode_page_cursor(
                    page_records[-1].created_at,
                    page_records[-1].external_session_id,
                )
                if has_more and page_records
                else None
            )
            return SessionPage(
                items=[_session_out(item) for item in page_records],
                next_cursor=next_cursor,
                has_more=has_more,
            )

    @app.put(
        "/v1/instances/{subject_ref}/sessions/{session_id}",
        response_model=SessionOut,
        **api_doc(
            summary="幂等确保 Session",
            description=(
                "在 ready Instance 中创建或取得一个 Pi Session。`session_id` 由调用方生成，并在"
                "该 Instance 内稳定唯一；网络超时后可使用相同 ID 和请求体安全重试。\n\n"
                "`model_slug` 必须由 Instance 当前 Policy 允许。`legacy_bridge_session_id` 和 "
                "可选 `cwd` 指定 Agent 的初始工作目录；省略时使用 "
                "`/root/workspace/sessions/{session_id}`。`legacy_bridge_session_id` 和 "
                "`legacy_cwd` 仅供旧数据迁移，新调用方不要设置。首次调用可能同步等待 Bridge "
                "创建会话，但不会运行 Agent Turn。\n\n需要 `sessions:write` scope。"
            ),
            tag="Session（会话）",
            operation_id="ensure_manager_session",
            response_description="已创建或已存在的 Session。",
        ),
    )
    async def put_session(
        payload: SessionEnsure,
        subject_ref: str,
        session_id: str,
        caller: Principal = Depends(service_principal),
    ) -> SessionOut:
        caller.require("sessions:write")
        connection, binding = _prepare_session(
            db_control, cipher, caller, subject_ref, session_id, payload
        )
        if binding.bridge_session_id is None:
            client = BridgeClient(resolved, connection.bridge_url, connection.bridge_token)
            try:
                bridge_id = _find_bridge_session(
                    client, binding.cwd, payload.legacy_bridge_session_id
                )
                if bridge_id is None:
                    created = await asyncio.to_thread(
                        client.create_session,
                        {
                            "name": payload.title,
                            "model": payload.model_slug,
                            "cwd": binding.cwd,
                        },
                    )
                    bridge_id = str(created["id"])
            except UpstreamProblem as exc:
                raise as_manager_problem(exc) from exc
            finally:
                client.close()
            with db_control.session() as db:
                current = db.get(SessionBinding, binding.id)
                if current is not None:
                    current.bridge_session_id = bridge_id
                    current.state = "ready"
                    binding = current
        return _session_out(binding)

    @app.get(
        "/v1/instances/{subject_ref}/sessions/{session_id}",
        response_model=SessionOut,
        **api_doc(
            summary="查询 Session",
            description=(
                "查询 Session，并同步刷新 Bridge 的运行状态。如果 Agent 已停止生成，Manager 会把"
                "对应 running Turn 收敛为 succeeded，并清除 `active_turn_id`。\n\n"
                "该读取因此会更新 Manager 状态；Instance 必须为 ready。需要 `sessions:read` scope。"
            ),
            tag="Session（会话）",
            operation_id="get_manager_session",
            response_description="Session 当前状态。",
        ),
    )
    async def get_session(
        subject_ref: str,
        session_id: str,
        caller: Principal = Depends(service_principal),
    ) -> SessionOut:
        caller.require("sessions:read")
        connection, binding = _session_connection(
            db_control, cipher, caller, subject_ref, session_id
        )
        if binding.bridge_session_id:
            client = BridgeClient(resolved, connection.bridge_url, connection.bridge_token)
            try:
                remote = await asyncio.to_thread(client.get_session, binding.bridge_session_id)
            except UpstreamProblem as exc:
                raise as_manager_problem(exc) from exc
            finally:
                client.close()
            is_streaming = bool(remote.get("is_streaming"))
            with db_control.session() as db:
                current = db.get(SessionBinding, binding.id)
                if current is not None:
                    current.state = "running" if is_streaming else "ready"
                    if not is_streaming and current.active_turn_id:
                        turn = db.scalar(
                            select(TurnBinding).where(
                                TurnBinding.session_binding_id == current.id,
                                TurnBinding.external_turn_id == current.active_turn_id,
                            )
                        )
                        if turn is not None and turn.status == "running":
                            turn.status = "succeeded"
                        current.active_turn_id = None
                    binding = current
        return _session_out(binding)

    @app.delete(
        "/v1/instances/{subject_ref}/sessions/{session_id}",
        status_code=204,
        **api_doc(
            summary="删除 Session",
            description=(
                "删除 Bridge 中的 Pi Session 以及 Manager 的绑定记录。"
                "Bridge 已不存在时仍按成功处理。"
                "调用方应先处理活动 Turn；删除后使用相同 `session_id` "
                "可以创建一个全新 Session。\n\n"
                "这不会删除整个 Instance 或其工作区命名卷。需要 `sessions:write` scope。"
            ),
            tag="Session（会话）",
            operation_id="delete_manager_session",
            response_description="删除成功，无响应体。",
        ),
    )
    async def delete_session(
        subject_ref: str,
        session_id: str,
        caller: Principal = Depends(service_principal),
    ) -> Response:
        caller.require("sessions:write")
        connection, binding = _session_connection(
            db_control, cipher, caller, subject_ref, session_id
        )
        if binding.bridge_session_id:
            client = BridgeClient(resolved, connection.bridge_url, connection.bridge_token)
            try:
                await asyncio.to_thread(client.delete_session, binding.bridge_session_id)
            except UpstreamProblem as exc:
                if exc.status_code != 404:
                    raise as_manager_problem(exc) from exc
            finally:
                client.close()
        with db_control.session() as db:
            current = db.get(SessionBinding, binding.id)
            if current is not None:
                db.delete(current)
        return Response(status_code=204)


    return get_session
