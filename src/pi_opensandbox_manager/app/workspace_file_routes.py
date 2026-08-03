from __future__ import annotations

import asyncio
from typing import Annotated, Any

from fastapi import Body, Depends, FastAPI, File, Form, Header, Query, Response, UploadFile

from ..clients import BridgeClient, UpstreamProblem, as_manager_problem
from ..config import ManagerSettings
from ..crypto import CredentialCipher
from ..database import ManagerDatabase
from ..openapi_docs import api_doc
from ..problems import ManagerProblem
from ..security import Principal
from .helpers import BridgeConnection, _connection, _owned_instance


def register_workspace_file_routes(
    app: FastAPI,
    database: ManagerDatabase,
    settings: ManagerSettings,
    cipher: CredentialCipher,
    service_dependency: Any,
) -> None:
    """Proxy the Bridge-enforced /root/workspace file boundary."""

    def require_path(path: str) -> str:
        if path.startswith("/") or "\x00" in path:
            raise ManagerProblem(422, "invalid_workspace_path", "path must be workspace-relative")
        return path

    def connection_for(subject_ref: str, caller: Principal, scope: str) -> BridgeConnection:
        caller.require(scope)
        with database.session() as db:
            return _connection(_owned_instance(db, caller, subject_ref), cipher)

    async def proxy(
        *,
        method: str,
        subject_ref: str,
        caller: Principal,
        scope: str,
        bridge_path: str,
        **kwargs: Any,
    ) -> Response:
        connection = connection_for(subject_ref, caller, scope)
        client = BridgeClient(settings, connection.bridge_url, connection.bridge_token)
        try:
            upstream = await asyncio.to_thread(client.passthrough, method, bridge_path, **kwargs)
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
                for name in (
                    "etag",
                    "content-disposition",
                    "content-range",
                    "x-file-size",
                    "x-file-modified-at",
                )
                if (value := upstream.headers.get(name)) is not None
            },
        )

    @app.get(
        "/v1/instances/{subject_ref}/workspace-files",
        **api_doc(
            summary="列出受限工作区目录",
            description=(
                "列出 Instance `/root/workspace` 内的相对路径。Bridge 会拒绝绝对路径、父目录跳转和"
                "符号链接逃逸；需要 `workspace:read` scope。"
            ),
            tag="Workspace（工作区）",
            operation_id="list_manager_workspace_files",
            response_description="工作区目录 JSON。",
        ),
    )
    async def list_files(
        subject_ref: str,
        path: Annotated[str, Query(max_length=4096)] = "",
        depth: Annotated[int, Query(ge=0, le=64)] = 1,
        caller: Principal = Depends(service_dependency),
    ) -> Response:
        return await proxy(
            method="GET",
            subject_ref=subject_ref,
            caller=caller,
            scope="workspace:read",
            bridge_path="/workspace-files",
            params={"path": require_path(path), "depth": depth},
        )

    @app.get(
        "/v1/instances/{subject_ref}/workspace-files/content",
        **api_doc(
            summary="读取受限工作区文件",
            description=(
                "读取 `/root/workspace` 内的相对常规文件，并返回内容、ETag、文件大小和修改时间。"
                "Content-Type 与 Content-Disposition 仅为文件元信息；需要 `workspace:read` scope。"
            ),
            tag="Workspace（工作区）",
            operation_id="read_manager_workspace_file",
            response_description="文件原始内容及文件元信息响应头。",
        ),
    )
    async def read_file(
        subject_ref: str,
        path: Annotated[str, Query(min_length=1, max_length=4096)],
        caller: Principal = Depends(service_dependency),
    ) -> Response:
        return await proxy(
            method="GET",
            subject_ref=subject_ref,
            caller=caller,
            scope="workspace:read",
            bridge_path="/workspace-files/content",
            params={"path": require_path(path)},
        )

    @app.put(
        "/v1/instances/{subject_ref}/workspace-files/content",
        status_code=204,
        **api_doc(
            summary="条件保存受限工作区文本文件",
            description=(
                "原子替换既有的 UTF-8 文本文件，最大 1 MiB。必须带 text/plain Content-Type 和"
                "完整读取取得的 If-Match ETag；需要 `workspace:write` scope。"
            ),
            tag="Workspace（工作区）",
            operation_id="update_manager_restricted_workspace_file",
            response_description="保存成功；ETag 为新版本。",
        ),
    )
    async def update_file(
        subject_ref: str,
        path: Annotated[str, Query(min_length=1, max_length=4096)],
        content: Annotated[bytes, Body(media_type="text/plain")],
        content_type: Annotated[str | None, Header(alias="Content-Type")] = None,
        if_match: Annotated[str | None, Header(alias="If-Match")] = None,
        caller: Principal = Depends(service_dependency),
    ) -> Response:
        headers = {
            key: value
            for key, value in {"Content-Type": content_type, "If-Match": if_match}.items()
            if value is not None
        }
        return await proxy(
            method="PUT",
            subject_ref=subject_ref,
            caller=caller,
            scope="workspace:write",
            bridge_path="/workspace-files/content",
            params={"path": require_path(path)},
            content=content,
            headers=headers or None,
        )

    @app.delete(
        "/v1/instances/{subject_ref}/workspace-files/content",
        status_code=204,
        **api_doc(
            summary="条件删除受限工作区文件",
            description=(
                "删除相对工作区常规文件，必须携带 If-Match ETag；需要 `workspace:write` scope。"
            ),
            tag="Workspace（工作区）",
            operation_id="delete_manager_restricted_workspace_file",
            response_description="文件已删除。",
        ),
    )
    async def delete_file(
        subject_ref: str,
        path: Annotated[str, Query(min_length=1, max_length=4096)],
        if_match: Annotated[str | None, Header(alias="If-Match")] = None,
        caller: Principal = Depends(service_dependency),
    ) -> Response:
        headers = {"If-Match": if_match} if if_match is not None else None
        return await proxy(
            method="DELETE",
            subject_ref=subject_ref,
            caller=caller,
            scope="workspace:write",
            bridge_path="/workspace-files/content",
            params={"path": require_path(path)},
            headers=headers,
        )

    @app.post(
        "/v1/instances/{subject_ref}/workspace-files/upload",
        status_code=201,
        **api_doc(
            summary="创建受限工作区文件",
            description=(
                "以 multipart/form-data 在工作区创建相对路径文件，必须提供 If-None-Match: *；"
                "需要 `workspace:write` scope。"
            ),
            tag="Workspace（工作区）",
            operation_id="upload_manager_workspace_file",
            response_description="文件已创建；ETag 为新版本。",
        ),
    )
    async def upload_file(
        subject_ref: str,
        path: Annotated[str, Form(min_length=1, max_length=4096)],
        file: Annotated[UploadFile, File()],
        if_none_match: Annotated[str | None, Header(alias="If-None-Match")] = None,
        caller: Principal = Depends(service_dependency),
    ) -> Response:
        headers = {"If-None-Match": if_none_match} if if_none_match is not None else None
        return await proxy(
            method="POST",
            subject_ref=subject_ref,
            caller=caller,
            scope="workspace:write",
            bridge_path="/workspace-files/upload",
            headers=headers,
            files={
                "file": (
                    file.filename or "upload",
                    await file.read(),
                    file.content_type or "application/octet-stream",
                )
            },
            data={"path": require_path(path)},
        )
