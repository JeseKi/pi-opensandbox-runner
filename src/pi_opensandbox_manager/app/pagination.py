from __future__ import annotations

import base64
import binascii
import json
from dataclasses import dataclass
from datetime import datetime

from ..problems import ManagerProblem


@dataclass(frozen=True)
class PageCursor:
    created_at: datetime
    item_id: str


def _encode_page_cursor(created_at: datetime, item_id: str) -> str:
    payload = json.dumps(
        [1, created_at.isoformat(), item_id],
        separators=(",", ":"),
    ).encode()
    return base64.urlsafe_b64encode(payload).decode().rstrip("=")


def _decode_page_cursor(value: str, resource: str) -> PageCursor:
    try:
        padding = "=" * (-len(value) % 4)
        decoded = base64.urlsafe_b64decode(value + padding).decode()
        payload = json.loads(decoded)
        if (
            not isinstance(payload, list)
            or len(payload) != 3
            or payload[0] != 1
            or not isinstance(payload[1], str)
            or not isinstance(payload[2], str)
            or not payload[2]
        ):
            raise ValueError("invalid cursor payload")
        return PageCursor(
            created_at=datetime.fromisoformat(payload[1]),
            item_id=payload[2],
        )
    except (
        binascii.Error,
        UnicodeDecodeError,
        json.JSONDecodeError,
        TypeError,
        ValueError,
    ) as exc:
        raise ManagerProblem(
            422,
            "invalid_cursor",
            f"{resource} cursor is invalid",
        ) from exc


