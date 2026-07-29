from __future__ import annotations

import asyncio
from typing import Any

from fastapi import Depends, FastAPI, Query

from ..clients import BridgeClient, UpstreamProblem, as_manager_problem
from ..config import ManagerSettings
from ..crypto import CredentialCipher
from ..database import ManagerDatabase
from ..openapi_docs import api_doc
from ..schemas import EventPage
from ..security import Principal
from .helpers import _event_out, _session_connection


def register_event_routes(
    app: FastAPI,
    db_control: ManagerDatabase,
    resolved: ManagerSettings,
    cipher: CredentialCipher,
    service_principal: Any,
) -> None:

    @app.get(
        "/v1/instances/{subject_ref}/sessions/{session_id}/events",
        response_model=EventPage,
        **api_doc(
            summary="增量读取 Session 事件",
            description=(
                "从 cursor 之后批量读取 Pi/Bridge 事件。首次从 `cursor=0` 开始，之后把响应中的 "
                "`next_cursor` 原样用于下一次请求；没有新事件时返回空 items 和不前进的 cursor。\n\n"
                "调用方必须按 `seq` 去重并持久化游标，以 `turn_id` 关联业务 Turn；未知 `type` 应"
                "忽略或透传，不能使消费循环失败。当前接口是轮询而非 SSE。\n\n"
                "需要 `sessions:read` scope。"
            ),
            tag="Turn（任务轮次）",
            operation_id="list_manager_session_events",
            response_description="事件批次和下一游标。",
        ),
    )
    async def events(
        subject_ref: str,
        session_id: str,
        cursor: int = Query(
            default=0,
            ge=0,
            description="上次响应 next_cursor 的整数值；首次请求使用 0。",
        ),
        limit: int = Query(
            default=100,
            ge=1,
            le=1000,
            description="单次最多返回的事件数，范围 1 至 1000。",
        ),
        caller: Principal = Depends(service_principal),
    ) -> EventPage:
        caller.require("sessions:read")
        connection, binding = _session_connection(
            db_control, cipher, caller, subject_ref, session_id
        )
        if not binding.bridge_session_id:
            return EventPage(items=[], next_cursor=str(cursor))
        client = BridgeClient(resolved, connection.bridge_url, connection.bridge_token)
        try:
            values, next_cursor = await asyncio.to_thread(
                client.event_batch, binding.bridge_session_id, cursor, limit
            )
        except UpstreamProblem as exc:
            raise as_manager_problem(exc) from exc
        finally:
            client.close()
        items = [_event_out(value, session_id, binding.active_turn_id) for value in values]
        return EventPage(items=items, next_cursor=str(next_cursor))

