from __future__ import annotations

import asyncio
from typing import Any

from fastapi import Depends, FastAPI, Response

from ..clients import BridgeClient, UpstreamProblem, as_manager_problem
from ..config import ManagerSettings
from ..crypto import CredentialCipher
from ..database import ManagerDatabase
from ..openapi_docs import api_doc
from ..schemas import CommandCreate
from ..security import Principal
from .helpers import _session_connection


def register_command_routes(
    app: FastAPI,
    database: ManagerDatabase,
    settings: ManagerSettings,
    cipher: CredentialCipher,
    service_dependency: Any,
) -> None:

    async def command_proxy(
        *,
        method: str,
        subject_ref: str,
        session_id: str,
        command_id: str,
        caller: Principal,
        payload: dict[str, Any] | None = None,
    ) -> Response:
        caller.require("commands:execute")
        connection, binding = _session_connection(
            database, cipher, caller, subject_ref, session_id
        )
        path = "/commands" + (f"/{command_id}" if command_id else "")
        kwargs: dict[str, Any] = {}
        if method == "POST":
            body = dict(payload or {})
            body["cwd"] = binding.cwd
            body["background"] = True
            kwargs["json"] = body
        client = BridgeClient(settings, connection.bridge_url, connection.bridge_token)
        try:
            upstream = await asyncio.to_thread(
                client.passthrough, method, path, **kwargs
            )
        except UpstreamProblem as exc:
            raise as_manager_problem(exc) from exc
        finally:
            client.close()
        return Response(
            upstream.content,
            status_code=upstream.status_code,
            media_type=upstream.headers.get("content-type", "application/json"),
        )

    @app.post(
        "/v1/instances/{subject_ref}/sessions/{session_id}/commands",
        status_code=202,
        **api_doc(
            summary="执行后台命令",
            description=(
                "预览能力。在 Session cwd 中通过 OpenSandbox Execd 执行 shell 命令，不进入 Pi "
                "对话上下文。Manager 会忽略请求中的 `cwd` 和 `background`，强制使用 Session cwd "
                "并以后台模式执行。\n\n"
                "命令进程拥有 Runner 容器的文件权限，workspace 前缀不是安全沙箱；调用方必须限制"
                "谁能提交命令。响应中的 command ID 用于查询或中止。需要 "
                "`commands:execute` scope。"
            ),
            tag="Command（后台命令）",
            operation_id="create_manager_command",
            response_description="OpenSandbox 接受后台命令后的 JSON。",
        ),
    )
    async def create_command(
        payload: CommandCreate,
        subject_ref: str,
        session_id: str,
        caller: Principal = Depends(service_dependency),
    ) -> Response:
        return await command_proxy(
            method="POST",
            subject_ref=subject_ref,
            session_id=session_id,
            command_id="",
            caller=caller,
            payload=payload.model_dump(exclude_none=True),
        )

    @app.get(
        "/v1/instances/{subject_ref}/sessions/{session_id}/commands/{command_id}",
        **api_doc(
            summary="查询后台命令状态",
            description=(
                "查询此前后台执行命令的状态和退出结果。command_id 来自执行命令响应。"
                "当前 Manager 尚未暴露独立日志游标接口；"
                "返回结构由 Bridge/OpenSandbox 协议决定。\n\n"
                "需要 `commands:execute` scope。"
            ),
            tag="Command（后台命令）",
            operation_id="get_manager_command",
            response_description="后台命令状态 JSON。",
        ),
    )
    async def get_command(
        subject_ref: str,
        session_id: str,
        command_id: str,
        caller: Principal = Depends(service_dependency),
    ) -> Response:
        return await command_proxy(
            method="GET",
            subject_ref=subject_ref,
            session_id=session_id,
            command_id=command_id,
            caller=caller,
        )

    @app.delete(
        "/v1/instances/{subject_ref}/sessions/{session_id}/commands/{command_id}",
        status_code=202,
        **api_doc(
            summary="中止后台命令",
            description=(
                "请求 OpenSandbox 中止指定后台命令。返回 202 只表示中止请求已接受；调用方应继续查询"
                "命令状态确认终态。command_id 来自执行命令响应。\n\n"
                "需要 `commands:execute` scope。"
            ),
            tag="Command（后台命令）",
            operation_id="cancel_manager_command",
            response_description="OpenSandbox 接受中止请求后的响应。",
        ),
    )
    async def cancel_command(
        subject_ref: str,
        session_id: str,
        command_id: str,
        caller: Principal = Depends(service_dependency),
    ) -> Response:
        return await command_proxy(
            method="DELETE",
            subject_ref=subject_ref,
            session_id=session_id,
            command_id=command_id,
            caller=caller,
        )


