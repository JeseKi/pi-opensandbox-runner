from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator

ThinkingLevel = Literal["off", "minimal", "low", "medium", "high", "xhigh", "max"]
DeliveryMode = Literal["auto", "steer", "follow_up"]


class SessionCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    provider: str | None = Field(default=None, min_length=1, max_length=80)
    model: str | None = Field(default=None, min_length=1, max_length=240)
    thinking_level: ThinkingLevel | None = None
    cwd: str | None = Field(default=None, min_length=1, max_length=4096)

    @field_validator("name")
    @classmethod
    def clean_name(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("name cannot be blank")
        return value

    @model_validator(mode="after")
    def paired_model(self) -> SessionCreate:
        if (self.provider is None) != (self.model is None):
            raise ValueError("provider and model must be supplied together")
        return self


class SessionPatch(BaseModel):
    name: str = Field(min_length=1, max_length=120)

    @field_validator("name")
    @classmethod
    def clean_name(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("name cannot be blank")
        return value


class SessionOut(BaseModel):
    id: str
    name: str
    cwd: str
    provider: str
    model: str
    thinking_level: str | None
    session_file: str | None
    materialized: bool
    state: str
    is_streaming: bool
    message_count: int
    leaf_id: str | None
    created_at: str
    updated_at: str
    last_error: str | None


class SessionPage(BaseModel):
    items: list[SessionOut]
    next_cursor: str | None
    has_more: bool


class PromptCreate(BaseModel):
    message: str = Field(min_length=1, max_length=100_000)
    delivery: DeliveryMode = "auto"
    provider: str | None = Field(default=None, min_length=1, max_length=80)
    model: str | None = Field(default=None, min_length=1, max_length=240)
    thinking_level: ThinkingLevel | None = None

    @field_validator("message")
    @classmethod
    def clean_message(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("message cannot be blank")
        return value

    @model_validator(mode="after")
    def paired_model(self) -> PromptCreate:
        if (self.provider is None) != (self.model is None):
            raise ValueError("provider and model must be supplied together")
        return self


class PromptAccepted(BaseModel):
    command_id: str
    session_id: str
    delivery: Literal["prompt", "steer", "follow_up"]


class CommandCreate(BaseModel):
    command: str = Field(min_length=1, max_length=100_000)
    cwd: str | None = Field(default=None, min_length=1, max_length=4096)
    timeout: int | None = Field(default=None, ge=1, le=86_400_000)
    background: bool = False
    envs: dict[str, str] | None = None
    uid: int | None = Field(default=None, ge=0)
    gid: int | None = Field(default=None, ge=0)

    @field_validator("command")
    @classmethod
    def clean_command(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("command cannot be blank")
        return value


class EntryPage(BaseModel):
    items: list[dict[str, Any]]
    next_cursor: str | None
    has_more: bool
    leaf_id: str | None
