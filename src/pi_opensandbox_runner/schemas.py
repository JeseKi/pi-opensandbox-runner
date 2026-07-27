from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator

ThinkingLevel = Literal["off", "minimal", "low", "medium", "high", "xhigh", "max"]
DeliveryMode = Literal["auto", "steer", "follow_up"]
SystemPromptMode = Literal["append", "replace"]


class SessionCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120, description="非空 Session 显示名称。")
    provider: str | None = Field(
        default=None,
        min_length=1,
        max_length=80,
        description="模型供应商；必须与 model 一起提供，或同时省略以使用容器默认值。",
    )
    model: str | None = Field(
        default=None,
        min_length=1,
        max_length=240,
        description="模型 ID；必须与 provider 一起提供。",
    )
    thinking_level: ThinkingLevel | None = Field(
        default=None,
        description="模型支持的思考强度；实际可用范围由所选模型决定。",
    )
    cwd: str | None = Field(
        default=None,
        min_length=1,
        max_length=4096,
        description="绝对容器路径；仅为 Pi 初始工作目录，不是权限边界。",
    )
    system_prompt: str | None = Field(
        default=None,
        min_length=1,
        max_length=100_000,
        description="可选的 Session 专属 system prompt，最大 100,000 字符。",
    )
    system_prompt_mode: SystemPromptMode = Field(
        default="append",
        description="append 追加到 Pi 默认 prompt；replace 完全替换 Pi 默认 prompt。",
    )

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
    name: str = Field(min_length=1, max_length=120, description="新的非空 Session 显示名称。")

    @field_validator("name")
    @classmethod
    def clean_name(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("name cannot be blank")
        return value


class SessionOut(BaseModel):
    id: str = Field(description="bridge 生成的稳定 Session UUID。")
    name: str = Field(description="Session 显示名称。")
    cwd: str = Field(description="Pi 初始工作目录，不限制 Pi 访问其他路径。")
    provider: str = Field(description="恢复该 Session 时使用的 provider。")
    model: str = Field(description="恢复该 Session 时使用的 model。")
    thinking_level: str | None = Field(description="持久化的模型思考强度。")
    system_prompt: str | None = Field(description="完整 Session 自定义 system prompt。")
    system_prompt_mode: SystemPromptMode = Field(description="自定义 prompt 的组合方式。")
    session_file: str | None = Field(description="Pi JSONL 历史文件；未产生对话时为空。")
    materialized: bool = Field(description="Pi JSONL 历史文件是否已存在。")
    state: str = Field(
        description="bridge 记录的 stopped、starting、running、idle 或 failed 状态。"
    )
    is_streaming: bool = Field(description="Pi 是否正在生成。")
    message_count: int = Field(description="当前可见分支中的消息 entry 数。")
    leaf_id: str | None = Field(description="当前 Pi 对话树叶 entry 的 id。")
    created_at: str = Field(description="ISO 8601 创建时间。")
    updated_at: str = Field(description="ISO 8601 最后更新时间。")
    last_error: str | None = Field(description="最近一次 Pi 进程失败信息。")


class SessionPage(BaseModel):
    items: list[SessionOut]
    next_cursor: str | None
    has_more: bool


class PromptCreate(BaseModel):
    message: str = Field(
        min_length=1, max_length=100_000, description="非空用户消息，最大 100,000 字符。"
    )
    delivery: DeliveryMode = Field(
        default="auto",
        description="auto 自动选择；steer/follow_up 仅在 Pi 正在生成时可用。",
    )
    provider: str | None = Field(
        default=None,
        min_length=1,
        max_length=80,
        description="本次切换的 provider，须与 model 成对提供。",
    )
    model: str | None = Field(
        default=None,
        min_length=1,
        max_length=240,
        description="本次切换的 model，须与 provider 成对提供。",
    )
    thinking_level: ThinkingLevel | None = Field(
        default=None, description="本次切换的思考强度，并持久化供恢复使用。"
    )

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


class SystemPromptUpdate(BaseModel):
    system_prompt: str = Field(
        min_length=1,
        max_length=100_000,
        description="完整替换保存的自定义 prompt；不能是空白文本。",
    )
    system_prompt_mode: SystemPromptMode = Field(
        default="append",
        description="append 保留 Pi 默认 prompt；replace 完全替换 Pi 默认 prompt。",
    )

    @field_validator("system_prompt")
    @classmethod
    def clean_system_prompt(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("system_prompt cannot be blank")
        return value


class CommandCreate(BaseModel):
    command: str = Field(
        min_length=1, max_length=100_000, description="在容器内执行的非空 shell 命令。"
    )
    cwd: str | None = Field(
        default=None,
        min_length=1,
        max_length=4096,
        description="命令工作目录；未设置时由 OpenSandbox 决定。",
    )
    timeout: int | None = Field(
        default=None, ge=1, le=86_400_000, description="超时毫秒数，最大 24 小时。"
    )
    background: bool = Field(default=False, description="true 时后台执行，使用状态和日志接口查询。")
    envs: dict[str, str] | None = Field(default=None, description="传给命令的环境变量。")
    uid: int | None = Field(default=None, ge=0, description="可选执行用户 UID。")
    gid: int | None = Field(default=None, ge=0, description="可选执行用户 GID。")

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
