from __future__ import annotations

import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

ThinkingLevel = Literal["off", "minimal", "low", "medium", "high", "xhigh", "max"]
DeliveryMode = Literal["auto", "steer", "follow_up"]
SystemPromptMode = Literal["append", "replace"]
McpTransport = Literal["streamable_http", "sse"]
MCP_ENV_REFERENCE = re.compile(r"\$\{(MCP_[A-Z0-9_]+)\}")
MCP_ANY_REFERENCE = re.compile(r"\$\{([^}]+)\}")


def validate_mcp_headers(headers: dict[str, str]) -> dict[str, str]:
    for name, template in headers.items():
        if not name.strip() or "\r" in name or "\n" in name:
            raise ValueError("MCP header names must be non-empty and cannot contain newlines")
        if "\r" in template or "\n" in template:
            raise ValueError("MCP header templates cannot contain newlines")
        for reference in MCP_ANY_REFERENCE.findall(template):
            if not reference.startswith("MCP_") or not MCP_ENV_REFERENCE.fullmatch(
                "${" + reference + "}"
            ):
                raise ValueError("MCP header templates may only reference ${MCP_*} variables")
    return headers


class SessionCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=120, description="非空 Session 显示名称。")
    model: str | None = Field(
        default=None,
        min_length=1,
        max_length=240,
        description="LiteLLM 模型别名；省略时使用容器默认值。",
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
    mcp_server_ids: list[str] = Field(
        default_factory=list,
        max_length=16,
        description="创建后绑定的 MCP Server UUID 列表；最多 16 个，仅远程 HTTP MCP。",
    )

    @field_validator("name")
    @classmethod
    def clean_name(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("name cannot be blank")
        return value

    @field_validator("mcp_server_ids")
    @classmethod
    def unique_mcp_server_ids(cls, value: list[str]) -> list[str]:
        if len(value) != len(set(value)):
            raise ValueError("mcp_server_ids must not contain duplicates")
        return value


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


class ModelCatalogOut(BaseModel):
    models: list[str]


class ModelCatalogConfigOut(BaseModel):
    config: dict[str, Any]
    models: list[str]
    fingerprint: str


class ModelCatalogReplaceOut(ModelCatalogConfigOut):
    migrated_session_count: int
    restart_on_next_request: bool = True


class PromptCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    message: str = Field(
        min_length=1, max_length=100_000, description="非空用户消息，最大 100,000 字符。"
    )
    delivery: DeliveryMode = Field(
        default="auto",
        description="auto 自动选择；steer/follow_up 仅在 Pi 正在生成时可用。",
    )
    model: str | None = Field(
        default=None,
        min_length=1,
        max_length=240,
        description="本次切换的 LiteLLM 模型别名。",
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


class PromptAccepted(BaseModel):
    command_id: str
    request_id: str
    session_id: str
    delivery: Literal["prompt", "steer", "follow_up"]


class TerminalCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    cwd: str = Field(
        min_length=1,
        max_length=4096,
        description="Terminal 初始工作目录；它不是文件权限边界。",
    )


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


class McpServerCreate(BaseModel):
    name: str = Field(
        min_length=1,
        max_length=64,
        pattern=r"^[A-Za-z][A-Za-z0-9_-]*$",
        description="容器内唯一的 MCP Server 名称；用于稳定生成 Pi 工具名。",
    )
    transport: McpTransport = Field(
        default="streamable_http",
        description="远程 MCP 传输：streamable_http（默认）或兼容旧服务的 sse。",
    )
    url: str = Field(
        min_length=1,
        max_length=4096,
        description="MCP 服务 URL；默认只允许 HTTPS，设置 MCP_ALLOW_INSECURE_HTTP=1 才允许 HTTP。",
    )
    headers: dict[str, str] = Field(
        default_factory=dict,
        max_length=64,
        description="HTTP Header 模板。仅可使用 ${MCP_*} 环境变量，不会保存实际 token。",
    )
    request_timeout_ms: int = Field(
        default=30_000,
        ge=1_000,
        le=120_000,
        description="单次 MCP 请求超时毫秒数，范围 1,000 至 120,000。",
    )

    @field_validator("headers")
    @classmethod
    def validate_headers(cls, value: dict[str, str]) -> dict[str, str]:
        return validate_mcp_headers(value)


class McpServerPatch(BaseModel):
    name: str | None = Field(
        default=None, min_length=1, max_length=64, pattern=r"^[A-Za-z][A-Za-z0-9_-]*$"
    )
    transport: McpTransport | None = None
    url: str | None = Field(default=None, min_length=1, max_length=4096)
    headers: dict[str, str] | None = Field(default=None, max_length=64)
    request_timeout_ms: int | None = Field(default=None, ge=1_000, le=120_000)

    @field_validator("headers")
    @classmethod
    def validate_headers(cls, value: dict[str, str] | None) -> dict[str, str] | None:
        return None if value is None else validate_mcp_headers(value)


class McpServerOut(BaseModel):
    id: str = Field(description="MCP Server UUID。")
    name: str = Field(description="容器内唯一的 MCP Server 名称。")
    transport: McpTransport = Field(description="远程 MCP 传输类型。")
    url: str = Field(description="远程 MCP URL。")
    headers: dict[str, str] = Field(description="未展开的 Header 模板，不包含 token。")
    request_timeout_ms: int = Field(description="单次 MCP 请求超时毫秒数。")
    created_at: str
    updated_at: str


class SessionMcpServersUpdate(BaseModel):
    server_ids: list[str] = Field(
        max_length=16,
        description="完整替换 Session 的 MCP Server 绑定；空数组解除全部绑定。",
    )

    @field_validator("server_ids")
    @classmethod
    def unique_server_ids(cls, value: list[str]) -> list[str]:
        if len(value) != len(set(value)):
            raise ValueError("server_ids must not contain duplicates")
        return value


class SessionMcpServersOut(BaseModel):
    session_id: str = Field(description="所属 Session UUID。")
    items: list[McpServerOut] = Field(
        description="当前已绑定的 MCP Server；只表示配置，不表示实时连接状态。"
    )


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
