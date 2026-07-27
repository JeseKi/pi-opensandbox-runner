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
    @router.get("/files")
    async def list_files(
        path: str = "/root/workspace",
        depth: Annotated[int, Query(ge=0, le=64)] = 1,
    ) -> Response:
        return await ctx.execd_request(
            "GET", "/directories/list", params={"path": path, "depth": depth}
        )

    @router.get("/files/content")
    async def read_file(
        path: str,
        offset: Annotated[int | None, Query(ge=1)] = None,
        limit: Annotated[int | None, Query(ge=1, le=100_000)] = None,
        range_header: Annotated[str | None, Header(alias="Range")] = None,
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

    @router.delete("/files", status_code=204)
    async def delete_file(
        path: str,
        if_match: Annotated[str | None, Header(alias="If-Match")] = None,
    ) -> Response:
        lock = await ctx.file_lock(path)
        async with lock:
            entry, content = await ctx.get_file_bytes(path)
            ctx.require_if_match(if_match, ctx.file_etag(content), entry)
            await ctx.execd_request("DELETE", "/files", params={"path": path})
        return Response(status_code=204)

    @router.put("/files/content", status_code=204)
    async def update_text_file(
        path: str,
        request: Request,
        if_match: Annotated[str | None, Header(alias="If-Match")] = None,
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

    @router.post("/files/upload", status_code=201)
    async def upload_file(
        path: Annotated[str, Form(min_length=1, max_length=4096)],
        file: Annotated[UploadFile, File()],
        if_none_match: Annotated[str | None, Header(alias="If-None-Match")] = None,
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
