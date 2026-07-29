from __future__ import annotations

from typing import Any

from fastapi import Request
from fastapi.responses import JSONResponse


class ManagerProblem(Exception):
    def __init__(
        self,
        status_code: int,
        code: str,
        detail: str,
        *,
        retryable: bool = False,
        component: str = "runner-manager",
        extra: dict[str, Any] | None = None,
    ):
        self.status_code = status_code
        self.code = code
        self.detail = detail
        self.retryable = retryable
        self.component = component
        self.extra = extra or {}


def problem_response(problem: ManagerProblem, request: Request) -> JSONResponse:
    request_id = getattr(request.state, "request_id", None)
    content: dict[str, Any] = {
        "type": f"https://runner-manager.local/problems/{problem.code}",
        "title": problem.code.replace("_", " "),
        "status": problem.status_code,
        "detail": problem.detail,
        "instance": request.url.path,
        "code": problem.code,
        "request_id": request_id,
        "component": problem.component,
        "retryable": problem.retryable,
    }
    content.update(problem.extra)
    return JSONResponse(
        status_code=problem.status_code,
        content=content,
        media_type="application/problem+json",
    )
