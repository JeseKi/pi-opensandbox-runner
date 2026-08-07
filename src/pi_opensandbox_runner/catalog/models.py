from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import Integer, String, Text
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


def session_record(item: SessionModel) -> SessionRecord:
    return SessionRecord(**{key: getattr(item, key) for key in SessionRecord.__dataclass_fields__})
