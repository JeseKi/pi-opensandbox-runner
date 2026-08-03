from __future__ import annotations

import asyncio
from typing import Any, Annotated

from fastapi import Body, Depends, FastAPI, File, Form, Header, Query, Response, UploadFile

from ..clients import BridgeClient, UpstreamProblem, as_manager_problem
from ..config import ManagerSettings
from ..crypto import CredentialCipher
from ..database import ManagerDatabase
from ..openapi_docs import api_doc
from ..problems import ManagerProblem
from ..security import Principal
from .helpers import _connection, _owned_instance


def register_filesystem_routes(
    app: FastAPI,
    database: ManagerDatabase,
    settings: ManagerSettings,
    cipher: CredentialCipher,
    service_dependency: Any,
) -> None:
    """Proxy full Runner-container file access for trusted consumers only."""

    def require_path(path: str) -> str:
        if not path.startswith("/") or "\x00" in path:
            raise ManagerProblem(
                422,
                "invalid_container_path",
                "path must be an absolute container path without NUL bytes",
            )
        return path

    def connection_for(subject_ref: str, caller: Principal):
        caller.require("filesystem:access")
        with database.session() as db:
            instance = _owned_instance(db, caller, subject_ref)
            return _connection(instance, cipher)

    async def proxy(
        *,
        method: str,
        subject_ref: str,
        caller: Principal,
        bridge_path: str,
        **kwargs: Any,
    ) -> Response:
        connection = connection_for(subject_ref, caller)
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
        "/v1/instances/{subject_ref}/filesystem",
        **api_doc(
            summary="列出 Runner 容器目录",
            description=(
                "列出 ready Instance 中绝对容器路径的目录内容。该接口可访问整个 Runner 容器，"
                "包括不属于任一 Session workspace 的文件；只应授予已完成最终用户授权的可信"
                "业务系统。需要 `filesystem:access` scope。"
            ),
            tag="Filesystem（容器文件系统）",
            operation_id="list_manager_filesystem",
            response_description="Bridge 返回的目录列表 JSON。",
        ),
    )
    async def list_filesystem(
        subject_ref: str,
        path: Annotated[str, Query(min_length=1, max_length=4096)] = "/",
        depth: Annotated[int, Query(ge=0, le=64)] = 1,
        caller: Principal = Depends(service_dependency),
    ) -> Response:
        return await proxy(
            method="GET",
            subject_ref=subject_ref,
            caller=caller,
            bridge_path="/files",
            params={"path": require_path(path), "depth": depth},
        )

    @app.get(
        "/v1/instances/{subject_ref}/filesystem/content",
        **api_doc(
            summary="读取或下载容器文件",
            description=(
                "读取绝对容器路径的文件内容。可使用标准 Range 或 offset/limit 局部读取，"
                "但两者不能组合；完整读取会透传强 ETag。需要 `filesystem:access` scope。"
            ),
            tag="Filesystem（容器文件系统）",
            operation_id="read_manager_filesystem_file",
            response_description="文件原始内容及 ETag、Content-Disposition、Content-Range 等响应头。",
        ),
    )
    async def read_file(
        subject_ref: str,
        path: Annotated[str, Query(min_length=1, max_length=4096)],
        offset: Annotated[int | None, Query(ge=1)] = None,
        limit: Annotated[int | None, Query(ge=1, le=100_000)] = None,
        range_header: Annotated[str | None, Header(alias="Range")] = None,
        caller: Principal = Depends(service_dependency),
    ) -> Response:
        if range_header is not None and (offset is not None or limit is not None):
            raise ManagerProblem(
                422,
                "invalid_file_range",
                "Range cannot be combined with offset or limit",
            )
        params: dict[str, Any] = {"path": require_path(path)}
        if offset is not None:
            params["offset"] = offset
        if limit is not None:
            params["limit"] = limit
        headers = {"Range": range_header} if range_header is not None else None
        return await proxy(
            method="GET",
            subject_ref=subject_ref,
            caller=caller,
            bridge_path="/files/content",
            params=params,
            headers=headers,
        )

    @app.put(
        "/v1/instances/{subject_ref}/filesystem/content",
        status_code=204,
        **api_doc(
            summary="条件保存容器文本文件",
            description=(
                "原子替换已有绝对路径的 UTF-8 文本文件。请求必须包含 `text/plain` Content-Type "
                "和完整读取取得的强 If-Match ETag；文本最大 1 MiB。需要 "
                "`filesystem:access` scope。"
            ),
            tag="Filesystem（容器文件系统）",
            operation_id="update_manager_filesystem_file",
            response_description="保存成功；ETag 响应头是新版本。",
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
        headers = {key: value for key, value in {
            "Content-Type": content_type,
            "If-Match": if_match,
        }.items() if value is not None}
        return await proxy(
            method="PUT",
            subject_ref=subject_ref,
            caller=caller,
            bridge_path="/files/content",
            params={"path": require_path(path)},
            content=content,
            headers=headers or None,
        )

    @app.delete(
        "/v1/instances/{subject_ref}/filesystem/content",
        status_code=204,
        **api_doc(
            summary="条件删除容器文件",
            description=(
                "删除已有绝对路径的常规文件，必须带完整读取取得的强 If-Match ETag；"
                "不支持递归删除目录。需要 `filesystem:access` scope。"
            ),
            tag="Filesystem（容器文件系统）",
            operation_id="delete_manager_filesystem_file",
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
            bridge_path="/files",
            params={"path": require_path(path)},
            headers=headers,
        )

    @app.post(
        "/v1/instances/{subject_ref}/filesystem/upload",
        status_code=201,
        **api_doc(
            summary="上传新容器文件",
            description=(
                "以 multipart/form-data 创建新文件。path 必须是绝对路径，也可指向现有目录；"
                "必须提供 If-None-Match: *，不会覆盖已有文件。需要 `filesystem:access` scope。"
            ),
            tag="Filesystem（容器文件系统）",
            operation_id="upload_manager_filesystem_file",
            response_description="文件已创建；ETag 响应头对应新内容。",
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
            bridge_path="/files/upload",
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
