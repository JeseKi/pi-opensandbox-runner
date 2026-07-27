from __future__ import annotations

import uuid
from contextlib import suppress
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, File, Form, Header, Query, Request, Response, UploadFile

from ..execd import ExecdError
from .context import BridgeContext
from .problems import ApiProblem


def register_file_routes(router: APIRouter, ctx: BridgeContext) -> None:
    @router.get(
        "/files",
        summary="列出目录内容",
        description=(
            "类似 ls；路径不受 workspace 限制，可访问 Pi 进程有权访问的任意容器路径。"
            "depth 最大为 64。"
        ),
    )
    async def list_files(
        path: Annotated[
            str, Query(description="要列出的绝对或相对容器目录路径。")
        ] = "/root/workspace",
        depth: Annotated[
            int, Query(ge=0, le=64, description="递归深度，范围 0 至 64，默认 1。")
        ] = 1,
    ) -> Response:
        return await ctx.execd_request(
            "GET", "/directories/list", params={"path": path, "depth": depth}
        )

    @router.get(
        "/files/content",
        summary="读取或下载文件",
        description=(
            "完整读取会返回强 ETag、文件大小和修改时间；将 ETag 用于条件保存或删除。"
            "Range 与 offset/limit 不能同时使用。"
        ),
    )
    async def read_file(
        path: Annotated[str, Query(description="容器内文件路径。")],
        offset: Annotated[
            int | None, Query(ge=1, description="文本浏览起始位置；不可与 Range 同用。")
        ] = None,
        limit: Annotated[
            int | None,
            Query(ge=1, le=100_000, description="最多读取的单位数；不可与 Range 同用。"),
        ] = None,
        range_header: Annotated[
            str | None,
            Header(
                alias="Range",
                description="标准 HTTP byte range；不可与 offset/limit 同用。",
            ),
        ] = None,
    ) -> Response:
        if range_header is not None and (offset is not None or limit is not None):
            raise ApiProblem(
                422, "invalid_file_range", "Range cannot be combined with offset or limit"
            )
        params: dict[str, Any] = {"path": path}
        if offset is not None:
            params["offset"] = offset
        if limit is not None:
            params["limit"] = limit
        headers = {"Range": range_header} if range_header is not None else None
        try:
            upstream = await ctx.execd.request(
                "GET", "/files/download", params=params, headers=headers
            )
        except ExecdError as exc:
            raise ApiProblem(exc.status_code, "execd_request_failed", exc.detail) from exc
        response_headers = {
            name: value
            for name in ("content-disposition", "content-range")
            if (value := upstream.headers.get(name)) is not None
        }
        if offset is None and limit is None and range_header is None:
            entry = await ctx.get_file_info(path)
            response_headers["ETag"] = ctx.file_etag(upstream.content)
            response_headers["X-File-Size"] = str(entry.get("size", ""))
            if (modified_at := entry.get("modified_at")) is not None:
                response_headers["X-File-Modified-At"] = str(modified_at)
        return Response(
            content=upstream.content,
            status_code=upstream.status_code,
            media_type=upstream.headers.get("content-type"),
            headers=response_headers,
        )

    @router.delete(
        "/files",
        status_code=204,
        summary="条件删除文件",
        description=(
            "必须提供完整读取时得到的 If-Match ETag。缺失返回 428，文件已变化返回 412。"
        ),
    )
    async def delete_file(
        path: Annotated[str, Query(description="要删除的现有常规文件路径。")],
        if_match: Annotated[
            str | None, Header(alias="If-Match", description="完整读取响应中的强 ETag；必填。")
        ] = None,
    ) -> Response:
        lock = await ctx.file_lock(path)
        async with lock:
            entry, content = await ctx.get_file_bytes(path)
            ctx.require_if_match(if_match, ctx.file_etag(content), entry)
            await ctx.execd_request("DELETE", "/files", params={"path": path})
        return Response(status_code=204)

    @router.put(
        "/files/content",
        status_code=204,
        summary="条件保存纯文本文件",
        description=(
            "请求体必须是 text/plain UTF-8，且目标与新内容均不得超过 1 MiB、不得含 NUL。"
            "必须提供 If-Match ETag；成功时以原子替换保存并返回新 ETag。"
        ),
    )
    async def update_text_file(
        path: Annotated[str, Query(description="要原子替换的现有常规文件路径。")],
        request: Request,
        if_match: Annotated[
            str | None, Header(alias="If-Match", description="完整读取响应中的强 ETag；必填。")
        ] = None,
    ) -> Response:
        content_type = request.headers.get("content-type", "")
        if not content_type.lower().startswith("text/plain"):
            raise ApiProblem(
                415,
                "unsupported_media_type",
                "Content-Type must be text/plain; charset=utf-8",
            )
        encoded_content = await request.body()
        if len(encoded_content) > ctx.max_text_file_bytes:
            raise ApiProblem(
                422,
                "not_editable_text_file",
                f"replacement content must be at most {ctx.max_text_file_bytes} bytes",
            )
        try:
            decoded_content = encoded_content.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ApiProblem(
                422, "not_editable_text_file", "replacement is not valid UTF-8 text"
            ) from exc
        if "\x00" in decoded_content:
            raise ApiProblem(422, "not_editable_text_file", "replacement contains NUL bytes")
        lock = await ctx.file_lock(path)
        async with lock:
            entry, current_content = await ctx.get_editable_text_file(path)
            ctx.require_if_match(if_match, ctx.file_etag(current_content), entry)
            temporary_path = f"{path}.pi-runner-{uuid.uuid4().hex}.tmp"
            try:
                await ctx.upload_bytes(
                    temporary_path,
                    encoded_content,
                    filename=Path(path).name or "file",
                    content_type="text/plain; charset=utf-8",
                    source_entry=entry,
                )
                await ctx.execd_request(
                    "POST",
                    "/command",
                    json_body={
                        "command": (
                            "python -c 'import os; "
                            "os.replace(os.environ[\"PI_RUNNER_TMP\"], "
                            "os.environ[\"PI_RUNNER_DEST\"])'"
                        ),
                        "envs": {"PI_RUNNER_TMP": temporary_path, "PI_RUNNER_DEST": path},
                        "timeout": 10_000,
                    },
                )
                _, written_content = await ctx.get_file_bytes(path)
                if ctx.file_etag(written_content) != ctx.file_etag(encoded_content):
                    raise ApiProblem(
                        502,
                        "atomic_replace_failed",
                        "replacement command completed without writing the expected content",
                    )
            except BaseException:
                with suppress(ApiProblem):
                    await ctx.execd_request("DELETE", "/files", params={"path": temporary_path})
                raise
        return Response(status_code=204, headers={"ETag": ctx.file_etag(encoded_content)})

    @router.post(
        "/files/upload",
        status_code=201,
        summary="上传新文件",
        description=(
            "仅创建，不覆盖已有文件。必须使用 multipart/form-data 并带 If-None-Match: *；"
            "path 可为目标文件，或以 / 结尾/现有目录来使用上传文件名。"
        ),
    )
    async def upload_file(
        path: Annotated[
            str,
            Form(
                min_length=1,
                max_length=4096,
                description="新文件目标路径，或现有目录/以 / 结尾的目录。",
            ),
        ],
        file: Annotated[UploadFile, File(description="要创建的文件内容。")],
        if_none_match: Annotated[
            str | None,
            Header(alias="If-None-Match", description="必须精确为 *，以禁止覆盖。"),
        ] = None,
    ) -> Response:
        if if_none_match != "*":
            raise ApiProblem(
                428,
                "precondition_required",
                "If-None-Match: * is required when creating a file",
            )
        destination = path
        if path.endswith("/"):
            destination = str(Path(path) / Path(file.filename or "upload").name)
        else:
            try:
                info_response = await ctx.execd.request("GET", "/files/info", params={"path": path})
            except ExecdError as exc:
                if exc.status_code != 404:
                    raise ApiProblem(exc.status_code, "execd_request_failed", exc.detail) from exc
            else:
                info = info_response.json()
                entry = info.get(path) if isinstance(info, dict) else None
                if isinstance(entry, dict) and entry.get("type") == "directory":
                    destination = str(Path(path) / Path(file.filename or "upload").name)
        lock = await ctx.file_lock(destination)
        async with lock:
            try:
                await ctx.get_file_info(destination)
            except ApiProblem as exc:
                if exc.status_code != 404:
                    raise
            else:
                raise ApiProblem(412, "file_exists", "a file already exists at the destination")
            content = await file.read()
            await ctx.upload_bytes(
                destination,
                content,
                filename=file.filename or "upload",
                content_type=file.content_type or "application/octet-stream",
            )
        return Response(status_code=201, headers={"ETag": ctx.file_etag(content)})
