from __future__ import annotations

from typing import Any

from fastapi import Request
from fastapi.responses import JSONResponse


class ApiProblem(Exception):
    def __init__(
        self,
        status_code: int,
        code: str,
        detail: str,
        *,
        extra: dict[str, Any] | None = None,
    ):
        self.status_code = status_code
        self.code = code
        self.detail = detail
        self.extra = extra or {}


def problem_response(problem: ApiProblem, request: Request) -> JSONResponse:
    content: dict[str, Any] = {
        "type": f"https://pi-runner.local/problems/{problem.code}",
        "title": problem.code.replace("_", " "),
        "status": problem.status_code,
        "detail": problem.detail,
        "instance": str(request.url.path),
        "code": problem.code,
    }
    content.update(problem.extra)
    return JSONResponse(
        status_code=problem.status_code,
        content=content,
        media_type="application/problem+json",
    )
