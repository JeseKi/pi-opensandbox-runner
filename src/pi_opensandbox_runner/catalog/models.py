from __future__ import annotations

import json
from dataclasses import dataclass

from sqlalchemy import ForeignKey, Integer, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class SessionModel(Base):
    __tablename__ = "sessions"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    name: Mapped[str] = mapped_column(String, nullable=False)
    cwd: Mapped[str] = mapped_column(String, nullable=False)
    model: Mapped[str] = mapped_column(String, nullable=False)
    thinking_level: Mapped[str | None] = mapped_column(String)
    system_prompt: Mapped[str | None] = mapped_column(Text)
    system_prompt_mode: Mapped[str] = mapped_column(String, nullable=False, default="append")
    session_file: Mapped[str | None] = mapped_column(String)
    created_at: Mapped[str] = mapped_column(String, nullable=False)
    updated_at: Mapped[str] = mapped_column(String, nullable=False, index=True)
    last_status: Mapped[str] = mapped_column(String, nullable=False)
    last_error: Mapped[str | None] = mapped_column(Text)
    event_seq: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


class McpServerModel(Base):
    __tablename__ = "mcp_servers"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    name: Mapped[str] = mapped_column(String, nullable=False, unique=True)
    transport: Mapped[str] = mapped_column(String, nullable=False)
    url: Mapped[str] = mapped_column(Text, nullable=False)
    headers_template: Mapped[str] = mapped_column(Text, nullable=False)
    request_timeout_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[str] = mapped_column(String, nullable=False)
    updated_at: Mapped[str] = mapped_column(String, nullable=False, index=True)


class SessionMcpServerModel(Base):
    __tablename__ = "session_mcp_servers"
    session_id: Mapped[str] = mapped_column(
        ForeignKey("sessions.id", ondelete="CASCADE"), primary_key=True
    )
    server_id: Mapped[str] = mapped_column(
        ForeignKey("mcp_servers.id", ondelete="CASCADE"), primary_key=True
    )


@dataclass(frozen=True)
class SessionRecord:
    id: str
    name: str
    cwd: str
    model: str
    thinking_level: str | None
    system_prompt: str | None
    system_prompt_mode: str
    session_file: str | None
    created_at: str
    updated_at: str
    last_status: str
    last_error: str | None
    event_seq: int


@dataclass(frozen=True)
class McpServerRecord:
    id: str
    name: str
    transport: str
    url: str
    headers_template: dict[str, str]
    request_timeout_ms: int
    created_at: str
    updated_at: str


def session_record(item: SessionModel) -> SessionRecord:
    return SessionRecord(**{key: getattr(item, key) for key in SessionRecord.__dataclass_fields__})


def mcp_server_record(item: McpServerModel) -> McpServerRecord:
    raw_headers = json.loads(item.headers_template)
    headers = raw_headers if isinstance(raw_headers, dict) else {}
    return McpServerRecord(
        id=item.id,
        name=item.name,
        transport=item.transport,
        url=item.url,
        headers_template={str(key): str(value) for key, value in headers.items()},
        request_timeout_ms=item.request_timeout_ms,
        created_at=item.created_at,
        updated_at=item.updated_at,
    )
