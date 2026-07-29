from __future__ import annotations

import asyncio
import json
from typing import Any
from uuid import uuid4

from fastapi import Depends, FastAPI
from sqlalchemy import select

from ..clients import BridgeClient, UpstreamProblem, as_manager_problem
from ..config import ManagerSettings
from ..crypto import CredentialCipher
from ..database import ManagerDatabase
from ..models import SessionBinding, TurnBinding
from ..openapi_docs import api_doc
from ..problems import ManagerProblem
from ..schemas import TurnOut, TurnSubmit
from ..security import Principal
from .helpers import _marked_input, _owned_session, _session_connection, _turn_out


def register_turn_routes(
    app: FastAPI,
    db_control: ManagerDatabase,
    resolved: ManagerSettings,
    cipher: CredentialCipher,
    service_principal: Any,
    refresh_session: Any,
) -> None:

    @app.put(
        "/v1/instances/{subject_ref}/sessions/{session_id}/turns/{turn_id}",
        response_model=TurnOut,
        status_code=202,
        **api_doc(
            summary="幂等提交 Agent Turn",
            description=(
                "把用户输入提交给指定 Pi Session。`turn_id` 由调用方生成，"
                "同时作为幂等键；调用方应先"
                "在自己的数据库中持久化任务与入队记录，再调用本接口。\n\n"
                "- 相同 Turn ID 与相同 input：返回已有 Turn，可安全重试。\n"
                "- 相同 Turn ID 与不同 input：返回 `409 idempotency_conflict`。\n"
                "- 同一 Session 已有活动 Turn：返回 `409 turn_active`。\n"
                "- 返回 `202/running` 只表示 Bridge 已接受；通过事件和 GET Turn 跟踪终态。\n\n"
                "需要 `sessions:write` scope。"
            ),
            tag="Turn（任务轮次）",
            operation_id="submit_manager_turn",
            response_description="已接受或幂等命中的 Turn。",
        ),
    )
    async def put_turn(
        payload: TurnSubmit,
        subject_ref: str,
        session_id: str,
        turn_id: str,
        caller: Principal = Depends(service_principal),
    ) -> TurnOut:
        caller.require("sessions:write")
        connection, binding = _session_connection(
            db_control, cipher, caller, subject_ref, session_id
        )
        if not binding.bridge_session_id:
            raise ManagerProblem(
                409,
                "session_not_ready",
                "runtime session is not ready",
                retryable=True,
            )
        with db_control.session() as db:
            current = db.get(SessionBinding, binding.id)
            assert current is not None
            turn = db.scalar(
                select(TurnBinding).where(
                    TurnBinding.session_binding_id == current.id,
                    TurnBinding.external_turn_id == turn_id,
                )
            )
            if turn is not None:
                if turn.input_text != payload.input:
                    raise ManagerProblem(
                        409,
                        "idempotency_conflict",
                        "turn id was already used with a different input",
                    )
                return _turn_out(turn)
            if current.active_turn_id:
                raise ManagerProblem(409, "turn_active", "another turn is already active")
            turn = TurnBinding(
                id=str(uuid4()),
                session_binding_id=current.id,
                external_turn_id=turn_id,
                input_text=payload.input,
                status="queued",
            )
            db.add(turn)
            db.flush()
            turn_db_id = turn.id
        client = BridgeClient(resolved, connection.bridge_url, connection.bridge_token)
        try:
            result = await asyncio.to_thread(
                client.prompt,
                binding.bridge_session_id,
                _marked_input(turn_id, payload.input),
                idempotency_key=turn_id,
            )
        except UpstreamProblem as exc:
            with db_control.session() as db:
                current_turn = db.get(TurnBinding, turn_db_id)
                if current_turn is not None:
                    current_turn.status = "failed"
                    current_turn.problem_json = json.dumps(
                        {
                            "status": exc.status_code,
                            "code": exc.code,
                            "detail": exc.detail,
                            "retryable": exc.retryable,
                        }
                    )
            raise as_manager_problem(exc) from exc
        finally:
            client.close()
        with db_control.session() as db:
            current_turn = db.get(TurnBinding, turn_db_id)
            current_session = db.get(SessionBinding, binding.id)
            assert current_turn is not None and current_session is not None
            current_turn.status = "running"
            current_turn.command_id = (
                str(result["command_id"]) if result.get("command_id") else None
            )
            current_session.state = "running"
            current_session.active_turn_id = turn_id
            return _turn_out(current_turn)

    @app.get(
        "/v1/instances/{subject_ref}/sessions/{session_id}/turns/{turn_id}",
        response_model=TurnOut,
        **api_doc(
            summary="查询 Turn",
            description=(
                "查询 Turn 状态。该接口会先刷新 Session 状态，"
                "因此 Agent 已停止生成时，running Turn "
                "可在本次读取中收敛为 succeeded。\n\n"
                "`succeeded`、`cancelled` 和 `failed` 是终态。事件内容仍需通过 events 接口读取。"
            ),
            tag="Turn（任务轮次）",
            operation_id="get_manager_turn",
            response_description="Turn 当前状态。",
        ),
    )
    async def get_turn(
        subject_ref: str,
        session_id: str,
        turn_id: str,
        caller: Principal = Depends(service_principal),
    ) -> TurnOut:
        await refresh_session(subject_ref, session_id, caller)
        with db_control.session() as db:
            binding = _owned_session(db, caller, subject_ref, session_id)
            turn = db.scalar(
                select(TurnBinding).where(
                    TurnBinding.session_binding_id == binding.id,
                    TurnBinding.external_turn_id == turn_id,
                )
            )
            if turn is None:
                raise ManagerProblem(404, "turn_not_found", "turn not found")
            return _turn_out(turn)

    @app.post(
        "/v1/instances/{subject_ref}/sessions/{session_id}/turns/{turn_id}:cancel",
        response_model=TurnOut,
        status_code=202,
        **api_doc(
            summary="取消活动 Turn",
            description=(
                "请求 Bridge 中止当前生成，并把 Manager Turn 标记为 cancelled。若 Bridge 已经停止，"
                "仍会收敛本地状态；不存在的 Turn 返回 404。\n\n"
                "取消是尽力而为操作，调用方仍应继续读取事件并刷新 Session。需要 "
                "`sessions:write` scope。"
            ),
            tag="Turn（任务轮次）",
            operation_id="cancel_manager_turn",
            response_description="取消后的 Turn 状态。",
        ),
    )
    async def cancel_turn(
        subject_ref: str,
        session_id: str,
        turn_id: str,
        caller: Principal = Depends(service_principal),
    ) -> TurnOut:
        caller.require("sessions:write")
        connection, binding = _session_connection(
            db_control, cipher, caller, subject_ref, session_id
        )
        if binding.active_turn_id == turn_id and binding.bridge_session_id:
            client = BridgeClient(resolved, connection.bridge_url, connection.bridge_token)
            try:
                await asyncio.to_thread(client.abort, binding.bridge_session_id)
            except UpstreamProblem as exc:
                if exc.status_code != 409:
                    raise as_manager_problem(exc) from exc
            finally:
                client.close()
        with db_control.session() as db:
            current = _owned_session(db, caller, subject_ref, session_id)
            turn = db.scalar(
                select(TurnBinding).where(
                    TurnBinding.session_binding_id == current.id,
                    TurnBinding.external_turn_id == turn_id,
                )
            )
            if turn is None:
                raise ManagerProblem(404, "turn_not_found", "turn not found")
            turn.status = "cancelled"
            if current.active_turn_id == turn_id:
                current.active_turn_id = None
                current.state = "ready"
            return _turn_out(turn)

