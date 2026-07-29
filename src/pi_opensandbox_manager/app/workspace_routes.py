from __future__ import annotations

import asyncio
from typing import Annotated, Any

from fastapi import Body, Depends, FastAPI, Header, Response

from ..clients import BridgeClient, UpstreamProblem, as_manager_problem
from ..config import ManagerSettings
from ..crypto import CredentialCipher
from ..database import ManagerDatabase
from ..openapi_docs import api_doc
from ..security import Principal
from .helpers import _session_connection


def register_workspace_routes(
    app: FastAPI,
    database: ManagerDatabase,
    settings: ManagerSettings,
    cipher: CredentialCipher,
    service_dependency: Any,
) -> None:

    async def workspace_proxy(
        *,
        method: str,
        subject_ref: str,
        session_id: str,
        path: str,
        caller: Principal,
        content: bytes | None = None,
        content_type: str | None = None,
        if_match: str | None = None,
    ) -> Response:
        scope = "workspace:read" if method == "GET" else "workspace:write"
        caller.require(scope)
        connection, binding = _session_connection(
            database, cipher, caller, subject_ref, session_id
        )
        target = f"{binding.cwd}/{path.lstrip('/')}".rstrip("/")
        upstream_path = "/files/content" if method != "GET" or path else "/files"
        kwargs: dict[str, Any] = {"params": {"path": target}}
        headers: dict[str, str] = {}
        if method == "PUT":
            kwargs["content"] = content or b""
            if content_type:
                headers["Content-Type"] = content_type
        if if_match:
            headers["If-Match"] = if_match
        if headers:
            kwargs["headers"] = headers
        client = BridgeClient(settings, connection.bridge_url, connection.bridge_token)
        try:
            upstream = await asyncio.to_thread(
                client.passthrough, method, upstream_path, **kwargs
            )
        except UpstreamProblem as exc:
            raise as_manager_problem(exc) from exc
        finally:
            client.close()
        return Response(
            upstream.content,
            status_code=upstream.status_code,
            media_type=upstream.headers.get("content-type"),
            headers={
                name: value
                for name in ("etag", "content-disposition", "content-range")
                if (value := upstream.headers.get(name))
            },
        )

    @app.get(
        "/v1/instances/{subject_ref}/sessions/{session_id}/workspace/{path:path}",
        **api_doc(
            summary="列出目录或读取文件",
            description=(
                "预览能力。`path` 为空时列出 Session cwd 的一级目录内容；`path` 非空时读取该路径的"
                "完整文件内容，并透传 ETag、Content-Disposition 和 Content-Range 响应头。\n\n"
                "Manager 会把相对 path 拼到 Session cwd，但当前路径前缀**不是强安全边界**，不得把"
                "此接口直接开放给不可信终端用户。当前尚未暴露 Range、offset/limit 和递归深度。\n\n"
                "需要 `workspace:read` scope，Instance 必须为 ready。"
            ),
            tag="Workspace（工作区）",
            operation_id="read_manager_workspace",
            response_description="目录 JSON 或文件原始内容，取决于 path。",
        ),
    )
    async def read_workspace(
        subject_ref: str,
        session_id: str,
        path: str = "",
        caller: Principal = Depends(service_dependency),
    ) -> Response:
        return await workspace_proxy(
            method="GET",
            subject_ref=subject_ref,
            session_id=session_id,
            path=path,
            caller=caller,
        )

    @app.put(
        "/v1/instances/{subject_ref}/sessions/{session_id}/workspace/{path:path}",
        status_code=204,
        **api_doc(
            summary="条件保存纯文本文件",
            description=(
                "预览能力。用请求体原子替换 Session cwd 下的现有文本文件。必须发送 "
                "`Content-Type: text/plain; charset=utf-8` 和此前完整读取返回的强 `If-Match` "
                "ETag；缺少 ETag 返回 428，文件已变化返回 412。\n\n"
                "目标和新内容均不能超过 1 MiB，必须是 UTF-8 且不能包含 NUL；当前不支持新建文件、"
                "二进制上传或分块写入。需要 `workspace:write` scope。"
            ),
            tag="Workspace（工作区）",
            operation_id="update_manager_workspace_file",
            response_description="保存成功，无响应体；ETag 头包含新版本。",
        ),
    )
    async def update_workspace_file(
        subject_ref: str,
        session_id: str,
        path: str,
        content: Annotated[
            bytes,
            Body(
                description="完整的 UTF-8 文本文件内容，最大 1 MiB。",
                media_type="text/plain",
            ),
        ],
        content_type: Annotated[
            str | None,
            Header(
                alias="Content-Type",
                description="必须以 text/plain 开头，建议 text/plain; charset=utf-8。",
            ),
        ] = None,
        if_match: Annotated[
            str | None,
            Header(alias="If-Match", description="此前完整读取响应中的强 ETag；必填。"),
        ] = None,
        caller: Principal = Depends(service_dependency),
    ) -> Response:
        return await workspace_proxy(
            method="PUT",
            subject_ref=subject_ref,
            session_id=session_id,
            path=path,
            caller=caller,
            content=content,
            content_type=content_type,
            if_match=if_match,
        )

    @app.delete(
        "/v1/instances/{subject_ref}/sessions/{session_id}/workspace/{path:path}",
        status_code=204,
        **api_doc(
            summary="条件删除文件",
            description=(
                "预览能力。删除 Session cwd 下的现有常规文件。必须发送此前完整读取返回的强 "
                "`If-Match` ETag；缺少 ETag 返回 428，文件已变化返回 412。\n\n"
                "当前不支持递归删除目录。需要 `workspace:write` scope。"
            ),
            tag="Workspace（工作区）",
            operation_id="delete_manager_workspace_file",
            response_description="删除成功，无响应体。",
        ),
    )
    async def delete_workspace_file(
        subject_ref: str,
        session_id: str,
        path: str,
        if_match: Annotated[
            str | None,
            Header(alias="If-Match", description="此前完整读取响应中的强 ETag；必填。"),
        ] = None,
        caller: Principal = Depends(service_dependency),
    ) -> Response:
        return await workspace_proxy(
            method="DELETE",
            subject_ref=subject_ref,
            session_id=session_id,
            path=path,
            caller=caller,
            if_match=if_match,
        )

