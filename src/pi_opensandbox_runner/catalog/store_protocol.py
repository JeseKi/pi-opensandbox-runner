from __future__ import annotations

from collections.abc import Callable
from typing import Protocol, TypeVar

from sqlalchemy.orm import Session

_Result = TypeVar("_Result")


class CatalogStore(Protocol):
    def utc_now(self) -> str: ...

    async def _run(self, operation: Callable[[Session], _Result]) -> _Result: ...
