from __future__ import annotations

import hashlib
import json
import secrets
from dataclasses import dataclass
from typing import Annotated
from uuid import uuid4

from fastapi import Depends
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import ManagerSettings
from .database import ManagerDatabase
from .models import Consumer, ManagerToken
from .problems import ManagerProblem

MANAGER_BEARER_AUTH = HTTPBearer(
    auto_error=False,
    description=(
        "Manager Bearer token。业务 `/v1/**` 使用 consumer service token；"
        "内部 `/admin/v1/**` 使用 admin token。这里只输入 token 本身，不要手写 `Bearer ` 前缀。"
    ),
)
ManagerBearerCredentials = Annotated[
    HTTPAuthorizationCredentials | None,
    Depends(MANAGER_BEARER_AUTH),
]


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


@dataclass(frozen=True)
class Principal:
    token_id: str
    kind: str
    consumer_id: str | None
    scopes: frozenset[str]

    def require(self, scope: str) -> None:
        if self.kind != "admin" and scope not in self.scopes:
            raise ManagerProblem(403, "insufficient_scope", f"missing scope: {scope}")


class Authenticator:
    def __init__(self, database: ManagerDatabase):
        self.database = database

    def principal(self, token: str | None) -> Principal:
        if not token:
            raise ManagerProblem(401, "missing_token", "bearer token is required")
        with self.database.session() as db:
            record = db.scalar(
                select(ManagerToken).where(
                    ManagerToken.token_hash == token_hash(token),
                    ManagerToken.active.is_(True),
                )
            )
            if record is None:
                raise ManagerProblem(401, "invalid_token", "invalid bearer token")
            return Principal(
                token_id=record.id,
                kind=record.kind,
                consumer_id=record.consumer_id,
                scopes=frozenset(json.loads(record.scopes_json)),
            )


def bootstrap(database: ManagerDatabase, settings: ManagerSettings) -> None:
    with database.session() as db:
        consumer = db.scalar(
            select(Consumer).where(Consumer.slug == settings.bootstrap_consumer_slug)
        )
        if consumer is None:
            consumer = Consumer(id=str(uuid4()), slug=settings.bootstrap_consumer_slug)
            db.add(consumer)
            db.flush()
        if settings.bootstrap_service_token:
            _ensure_token(
                db,
                token=settings.bootstrap_service_token,
                consumer_id=consumer.id,
                kind="service",
                scopes=[
                    "catalog:read",
                    "instances:read",
                    "instances:write",
                    "sessions:read",
                    "sessions:write",
                    "workspace:read",
                    "workspace:write",
                    "commands:execute",
                ],
            )
        if settings.bootstrap_admin_token:
            _ensure_token(
                db,
                token=settings.bootstrap_admin_token,
                consumer_id=None,
                kind="admin",
                scopes=["*"],
            )


def _ensure_token(
    db: Session,
    *,
    token: str,
    consumer_id: str | None,
    kind: str,
    scopes: list[str],
) -> None:
    digest = token_hash(token)
    existing = db.scalar(select(ManagerToken).where(ManagerToken.token_hash == digest))
    if existing is None:
        db.add(
            ManagerToken(
                id=str(uuid4()),
                consumer_id=consumer_id,
                token_hash=digest,
                kind=kind,
                scopes_json=json.dumps(scopes),
            )
        )


def generate_token(prefix: str) -> str:
    return f"{prefix}_{secrets.token_urlsafe(32)}"
