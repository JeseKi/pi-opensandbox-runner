from __future__ import annotations

import json
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import (
    McpServerModel,
    McpServerRecord,
    SessionMcpServerModel,
    SessionModel,
    mcp_server_record,
)
from .store_protocol import CatalogStore


class McpServerStore(CatalogStore):
    async def create_mcp_server(
        self,
        *,
        server_id: str,
        name: str,
        transport: str,
        url: str,
        headers_template: dict[str, str],
        request_timeout_ms: int,
    ) -> McpServerRecord:
        now = self.utc_now()

        def operation(db: Session) -> McpServerRecord:
            item = McpServerModel(
                id=server_id,
                name=name,
                transport=transport,
                url=url,
                headers_template=json.dumps(
                    headers_template, sort_keys=True, separators=(",", ":")
                ),
                request_timeout_ms=request_timeout_ms,
                created_at=now,
                updated_at=now,
            )
            db.add(item)
            db.flush()
            return mcp_server_record(item)

        return await self._run(operation)

    async def get_mcp_server(self, server_id: str) -> McpServerRecord | None:
        return await self._run(
            lambda db: (
                None
                if (item := db.get(McpServerModel, server_id)) is None
                else mcp_server_record(item)
            )
        )

    async def list_mcp_servers(self) -> list[McpServerRecord]:
        return await self._run(
            lambda db: [
                mcp_server_record(item)
                for item in db.scalars(select(McpServerModel).order_by(McpServerModel.name))
            ]
        )

    async def update_mcp_server(self, server_id: str, **values: Any) -> McpServerRecord | None:
        def operation(db: Session) -> McpServerRecord | None:
            item = db.get(McpServerModel, server_id)
            if item is None:
                return None
            if "headers_template" in values:
                values["headers_template"] = json.dumps(
                    values["headers_template"], sort_keys=True, separators=(",", ":")
                )
            for key, value in values.items():
                setattr(item, key, value)
            item.updated_at = self.utc_now()
            db.flush()
            return mcp_server_record(item)

        return await self._run(operation)

    async def delete_mcp_server(self, server_id: str) -> bool | None:
        def operation(db: Session) -> bool | None:
            item = db.get(McpServerModel, server_id)
            if item is None:
                return None
            linked = db.scalar(
                select(SessionMcpServerModel.session_id).where(
                    SessionMcpServerModel.server_id == server_id
                )
            )
            if linked is not None:
                return False
            db.delete(item)
            return True

        return await self._run(operation)

    async def set_session_mcp_servers(
        self, session_id: str, server_ids: list[str]
    ) -> list[McpServerRecord] | None:
        def operation(db: Session) -> list[McpServerRecord] | None:
            if db.get(SessionModel, session_id) is None:
                return None
            items = [db.get(McpServerModel, server_id) for server_id in server_ids]
            if any(item is None for item in items):
                raise KeyError("mcp server does not exist")
            for binding in db.scalars(
                select(SessionMcpServerModel).where(SessionMcpServerModel.session_id == session_id)
            ):
                db.delete(binding)
            for server_id in server_ids:
                db.add(SessionMcpServerModel(session_id=session_id, server_id=server_id))
            db.flush()
            return [mcp_server_record(item) for item in items if item is not None]

        return await self._run(operation)

    async def get_session_mcp_servers(self, session_id: str) -> list[McpServerRecord] | None:
        def operation(db: Session) -> list[McpServerRecord] | None:
            if db.get(SessionModel, session_id) is None:
                return None
            stmt = (
                select(McpServerModel)
                .join(SessionMcpServerModel, SessionMcpServerModel.server_id == McpServerModel.id)
                .where(SessionMcpServerModel.session_id == session_id)
                .order_by(McpServerModel.name)
            )
            return [mcp_server_record(item) for item in db.scalars(stmt)]

        return await self._run(operation)
