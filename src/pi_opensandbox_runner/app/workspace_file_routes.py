from __future__ import annotations

import asyncio
import errno
import mimetypes
import os
import stat
from datetime import UTC, datetime
from typing import Annotated, Any
from urllib.parse import quote

from fastapi import APIRouter, File, Form, Header, Query, Request, Response, UploadFile

from .context import BridgeContext
from .problems import ApiProblem


def register_workspace_file_routes(router: APIRouter, ctx: BridgeContext) -> None:
    """Expose a symlink-safe, relative view of the instance workspace."""

    def parts(path: str) -> tuple[str, ...]:
        if "\x00" in path or path.startswith("/"):
            raise ApiProblem(
                422, "invalid_workspace_path", "path must be relative to the workspace"
            )
        value = tuple(path.split("/"))
        if any(part in {"", ".", ".."} for part in value):
            raise ApiProblem(422, "invalid_workspace_path", "path must not contain dot components")
        return value

    def open_directory(path_parts: tuple[str, ...]) -> int:
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        fd = os.open(ctx.settings.workspace_root, flags)
        try:
            for part in path_parts:
                next_fd = os.open(part, flags, dir_fd=fd)
                os.close(fd)
                fd = next_fd
            return fd
        except OSError:
            os.close(fd)
            raise

    def open_parent(path_parts: tuple[str, ...]) -> tuple[int, str]:
        if not path_parts:
            raise ApiProblem(422, "invalid_workspace_path", "path must name a file")
        return open_directory(path_parts[:-1]), path_parts[-1]

    def map_os_error(exc: OSError) -> ApiProblem:
        if exc.errno == errno.ENOENT:
            return ApiProblem(404, "file_not_found", "workspace file does not exist")
        if exc.errno == errno.EEXIST:
            return ApiProblem(412, "file_exists", "a file already exists at the destination")
        if exc.errno in {errno.ELOOP, errno.ENOTDIR, errno.EPERM, errno.EACCES}:
            return ApiProblem(422, "workspace_path_forbidden", "path leaves the workspace boundary")
        return ApiProblem(502, "workspace_file_failed", str(exc))

    def entry(name: str, info: os.stat_result) -> dict[str, Any]:
        mode = info.st_mode
        if stat.S_ISREG(mode):
            kind = "file"
        elif stat.S_ISDIR(mode):
            kind = "directory"
        elif stat.S_ISLNK(mode):
            kind = "symlink"
        else:
            kind = "other"
        return {
            "name": name,
            "type": kind,
            "size": info.st_size,
            "modified_at": datetime.fromtimestamp(info.st_mtime, UTC).isoformat(),
        }

    def list_directory(path_parts: tuple[str, ...], depth: int) -> dict[str, Any]:
        fd = open_directory(path_parts)
        try:
            items: list[dict[str, Any]] = []
            for name in sorted(os.listdir(fd)):
                info = os.stat(name, dir_fd=fd, follow_symlinks=False)
                item = entry(name, info)
                if depth > 0 and item["type"] == "directory":
                    try:
                        item["items"] = list_directory((*path_parts, name), depth - 1)["items"]
                    except OSError:
                        item["items"] = []
                items.append(item)
            return {"path": "/".join(path_parts), "items": items}
        finally:
            os.close(fd)

    def read_file(path_parts: tuple[str, ...]) -> tuple[bytes, os.stat_result]:
        parent_fd, name = open_parent(path_parts)
        try:
            fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=parent_fd)
        finally:
            os.close(parent_fd)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode):
                raise ApiProblem(422, "not_a_file", "path must identify a regular file")
            content = b"".join(iter(lambda: os.read(fd, 65_536), b""))
            return content, info
        finally:
            os.close(fd)

    def content_headers(
        path_parts: tuple[str, ...], content: bytes, info: os.stat_result
    ) -> dict[str, str]:
        filename = path_parts[-1]
        media_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
        return {
            "Content-Type": media_type,
            "Content-Disposition": f"attachment; filename*=UTF-8''{quote(filename, safe='')}",
            "ETag": ctx.file_etag(content),
            "X-File-Size": str(info.st_size),
            "X-File-Modified-At": datetime.fromtimestamp(info.st_mtime, UTC).isoformat(),
        }

    def replace_file(path_parts: tuple[str, ...], content: bytes, if_match: str | None) -> str:
        parent_fd, name = open_parent(path_parts)
        try:
            current_fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=parent_fd)
            try:
                info = os.fstat(current_fd)
                if not stat.S_ISREG(info.st_mode):
                    raise ApiProblem(422, "not_a_file", "path must identify a regular file")
                current = b"".join(iter(lambda: os.read(current_fd, 65_536), b""))
            finally:
                os.close(current_fd)
            ctx.require_if_match(if_match, ctx.file_etag(current), entry(name, info))
            temporary = f".{name}.pi-runner-{os.urandom(8).hex()}.tmp"
            temp_fd = os.open(
                temporary,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                stat.S_IMODE(info.st_mode),
                dir_fd=parent_fd,
            )
            try:
                os.write(temp_fd, content)
                os.fsync(temp_fd)
            finally:
                os.close(temp_fd)
            os.replace(temporary, name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
            return ctx.file_etag(content)
        finally:
            os.close(parent_fd)

    def create_file(path_parts: tuple[str, ...], content: bytes) -> str:
        parent_fd, name = open_parent(path_parts)
        try:
            fd = os.open(
                name,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o600,
                dir_fd=parent_fd,
            )
            try:
                os.write(fd, content)
                os.fsync(fd)
            finally:
                os.close(fd)
            return ctx.file_etag(content)
        finally:
            os.close(parent_fd)

    def remove_file(path_parts: tuple[str, ...], if_match: str | None) -> None:
        parent_fd, name = open_parent(path_parts)
        try:
            fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=parent_fd)
            try:
                info = os.fstat(fd)
                if not stat.S_ISREG(info.st_mode):
                    raise ApiProblem(422, "not_a_file", "path must identify a regular file")
                content = b"".join(iter(lambda: os.read(fd, 65_536), b""))
            finally:
                os.close(fd)
            ctx.require_if_match(if_match, ctx.file_etag(content), entry(name, info))
            os.unlink(name, dir_fd=parent_fd)
        finally:
            os.close(parent_fd)

    async def locked(path: str, action: Any) -> Any:
        lock = await ctx.file_lock(f"workspace:{path}")
        async with lock:
            try:
                return await asyncio.to_thread(action)
            except ApiProblem:
                raise
            except OSError as exc:
                raise map_os_error(exc) from exc

    @router.get("/workspace-files", summary="列出受限工作区目录")
    async def list_files(
        path: Annotated[str, Query(max_length=4096)] = "",
        depth: Annotated[int, Query(ge=0, le=64)] = 1,
    ) -> dict[str, Any]:
        path_parts = parts(path) if path else ()
        try:
            return await asyncio.to_thread(list_directory, path_parts, depth)
        except OSError as exc:
            raise map_os_error(exc) from exc

    @router.get("/workspace-files/content", summary="读取受限工作区文件")
    async def get_content(path: Annotated[str, Query(min_length=1, max_length=4096)]) -> Response:
        path_parts = parts(path)
        try:
            content, info = await asyncio.to_thread(read_file, path_parts)
        except OSError as exc:
            raise map_os_error(exc) from exc
        headers = content_headers(path_parts, content, info)
        return Response(content=content, headers=headers, media_type=headers.pop("Content-Type"))

    @router.put("/workspace-files/content", status_code=204, summary="条件保存工作区文本文件")
    async def put_content(
        path: Annotated[str, Query(min_length=1, max_length=4096)],
        request: Request,
        if_match: Annotated[str | None, Header(alias="If-Match")] = None,
    ) -> Response:
        if not request.headers.get("content-type", "").lower().startswith("text/plain"):
            raise ApiProblem(
                415, "unsupported_media_type", "Content-Type must be text/plain; charset=utf-8"
            )
        content = await request.body()
        if len(content) > ctx.max_text_file_bytes:
            raise ApiProblem(
                422,
                "not_editable_text_file",
                "replacement content must be at most 1048576 bytes",
            )
        try:
            text = content.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ApiProblem(
                422, "not_editable_text_file", "replacement is not valid UTF-8 text"
            ) from exc
        if "\x00" in text:
            raise ApiProblem(422, "not_editable_text_file", "replacement contains NUL bytes")
        etag = await locked(path, lambda: replace_file(parts(path), content, if_match))
        return Response(status_code=204, headers={"ETag": etag})

    @router.delete("/workspace-files/content", status_code=204, summary="条件删除工作区文件")
    async def delete_content(
        path: Annotated[str, Query(min_length=1, max_length=4096)],
        if_match: Annotated[str | None, Header(alias="If-Match")] = None,
    ) -> Response:
        await locked(path, lambda: remove_file(parts(path), if_match))
        return Response(status_code=204)

    @router.post("/workspace-files/upload", status_code=201, summary="创建工作区文件")
    async def upload_file(
        path: Annotated[str, Form(min_length=1, max_length=4096)],
        file: Annotated[UploadFile, File()],
        if_none_match: Annotated[str | None, Header(alias="If-None-Match")] = None,
    ) -> Response:
        if if_none_match != "*":
            raise ApiProblem(428, "precondition_required", "If-None-Match: * is required")
        content = await file.read()
        etag = await locked(path, lambda: create_file(parts(path), content))
        return Response(status_code=201, headers={"ETag": etag})
