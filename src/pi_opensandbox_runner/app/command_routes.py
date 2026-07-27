from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import APIRouter, Query, Response
from fastapi.responses import StreamingResponse

from ..execd import ExecdError
from ..schemas import CommandCreate
from .context import BridgeContext
from .problems import ApiProblem


def register_command_routes(router: APIRouter, ctx: BridgeContext) -> None:
    @router.post("/commands")
    async def run_command(payload: CommandCreate) -> Response:
        try:
            upstream = await ctx.execd.stream(
                "POST", "/command", json=payload.model_dump(exclude_none=True)
            )
        except ExecdError as exc:
            raise ApiProblem(exc.status_code, "execd_request_failed", exc.detail) from exc

        async def body() -> AsyncIterator[bytes]:
            try:
                async for chunk in upstream.aiter_bytes():
                    yield chunk
            finally:
                await upstream.aclose()

        return StreamingResponse(
            body(),
            status_code=upstream.status_code,
            media_type=upstream.headers.get("content-type", "text/event-stream"),
        )

    @router.delete("/commands/{command_id}", status_code=202)
    async def abort_command(command_id: str) -> Response:
        return await ctx.execd_request("DELETE", "/command", params={"id": command_id})

    @router.get("/commands/{command_id}")
    async def command_status(command_id: str) -> Response:
        return await ctx.execd_request("GET", f"/command/status/{command_id}")

    @router.get("/commands/{command_id}/logs")
    async def command_logs(
        command_id: str,
        cursor: Annotated[int | None, Query(ge=0)] = None,
    ) -> Response:
        params = {"cursor": cursor} if cursor is not None else None
        return await ctx.execd_request("GET", f"/command/{command_id}/logs", params=params)
