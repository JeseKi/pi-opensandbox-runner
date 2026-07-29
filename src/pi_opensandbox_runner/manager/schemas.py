from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class ProblemOut(BaseModel):
    type: str = Field(description="该错误类型的稳定 URI。")
    title: str = Field(description="供人阅读的简短错误标题；程序不要依赖此字段。")
    status: int = Field(description="与 HTTP 响应状态一致。", examples=[409])
    detail: str = Field(description="供诊断使用的错误详情；程序不要解析文本。")
    instance: str = Field(description="发生错误的请求路径。")
    code: str = Field(
        description="稳定、可供程序判断的错误码。",
        examples=["instance_not_ready"],
    )
    request_id: str | None = Field(
        default=None,
        description="请求追踪 ID；排查问题时应随日志一起提供。",
    )
    component: str = Field(default="runner-manager", description="报告错误的组件。")
    retryable: bool = Field(
        default=False,
        description="是否建议调用方在退避后重试；仍应结合 HTTP 状态与幂等性判断。",
    )
    errors: list[dict[str, Any]] | None = Field(
        default=None,
        description="请求校验失败时可能返回的字段级错误列表。",
    )


class ModelOut(BaseModel):
    slug: str = Field(description="业务系统和 Session 使用的稳定模型标识。")
    label: str = Field(description="适合管理界面展示的模型名称。")
    provider_model: str = Field(
        description="Manager 内部对应的提供商模型名；它不是传给 Session 的 model_slug。"
    )
    api: str = Field(description="LiteLLM/提供商使用的 API 协议类型。")
    context_window: int = Field(description="模型声明的最大上下文窗口。")
    max_tokens: int = Field(description="单次生成允许的最大输出 token 数。")
    reasoning: bool = Field(description="模型是否支持 reasoning/thinking 能力。")

    model_config = ConfigDict(from_attributes=True)


class PolicyOut(BaseModel):
    slug: str = Field(description="业务系统保存并传给 Instance ensure 的稳定 Policy 标识。")
    label: str = Field(description="适合浅层管理界面展示的 Policy 名称。")
    revision: int = Field(description="当前最新已发布 revision；相同 slug 可持续发布新 revision。")
    models: list[str] = Field(description="该 Policy 允许 Session 使用的 model slug。")
    default_model_slug: str = Field(description="该 Policy 的默认 model slug。")
    state: str = Field(description="发布状态；Catalog 当前只返回 published。")


class InstanceEnsure(BaseModel):
    policy_slug: str = Field(
        min_length=1,
        max_length=120,
        description="从 `/v1/catalog/policies` 选择的已发布 Policy slug。",
        examples=["consumer-default"],
    )

    model_config = ConfigDict(
        json_schema_extra={"examples": [{"policy_slug": "consumer-default"}]}
    )


class InstanceOut(BaseModel):
    id: str = Field(description="Manager 生成的内部 Instance UUID。")
    subject_ref: str = Field(description="调用方提供的稳定用户/主体标识。")
    policy_slug: str = Field(description="当前应用的 Policy slug。")
    policy_revision: int = Field(description="当前应用的 Policy revision。")
    state: str = Field(
        description=(
            "Instance 生命周期状态：provisioning、ready、stopping、stopped、"
            "destroying、destroyed 或 failed。"
        )
    )
    phase: str = Field(description="当前后台操作阶段；用于展示和诊断，不应作为业务枚举固化。")
    problem: dict[str, Any] | None = Field(
        description="最近一次失败的结构化问题；无失败时为 null。"
    )
    created_at: datetime = Field(description="创建时间，ISO 8601。")
    updated_at: datetime = Field(description="最后更新时间，ISO 8601。")
    ready_at: datetime | None = Field(description="最近一次进入 ready 的时间。")


class InstancePage(BaseModel):
    items: list[InstanceOut] = Field(description="按 created_at、id 倒序排列的 Instance。")
    next_cursor: str | None = Field(
        description="存在下一页时返回的不透明游标；调用方必须原样传回。"
    )
    has_more: bool = Field(description="是否还有下一页。")


class OperationOut(BaseModel):
    id: str = Field(description="用于轮询的异步 Operation UUID。")
    kind: str = Field(description="操作类型：provision、stop 或 destroy。")
    status: str = Field(
        description="pending、running、succeeded 或 failed；终态后停止轮询。"
    )
    phase: str = Field(description="更细的执行阶段；用于进度展示和诊断。")
    attempt: int = Field(description="已开始的执行次数；瞬时失败可能触发退避重试。")
    problem: dict[str, Any] | None = Field(description="最终或最近一次失败信息。")
    created_at: datetime = Field(description="创建时间，ISO 8601。")
    updated_at: datetime = Field(description="最后更新时间，ISO 8601。")
    finished_at: datetime | None = Field(description="进入 succeeded/failed 终态的时间。")


class AcceptedOperation(BaseModel):
    instance: InstanceOut = Field(description="接受操作后的 Instance 快照。")
    operation: OperationOut = Field(description="需要轮询的异步 Operation。")


class SessionEnsure(BaseModel):
    title: str = Field(
        min_length=1,
        max_length=160,
        description="会话显示标题。",
        examples=["修复项目测试"],
    )
    model_slug: str = Field(
        min_length=1,
        max_length=120,
        description="Policy 允许的 model slug；可从 Catalog 查询。",
        examples=["coding-default"],
    )
    legacy_bridge_session_id: str | None = Field(
        default=None,
        max_length=120,
        description="仅旧数据迁移使用；新调用方必须省略。",
    )
    legacy_cwd: str | None = Field(
        default=None,
        max_length=4096,
        description="仅旧数据迁移使用；新调用方必须省略。",
    )

    model_config = ConfigDict(
        json_schema_extra={
            "examples": [{"title": "修复项目测试", "model_slug": "coding-default"}]
        }
    )


class SessionOut(BaseModel):
    id: str = Field(description="调用方提供的稳定 Session ID。")
    state: str = Field(description="provisioning、ready、running 或 failed。")
    title: str = Field(description="会话显示标题。")
    model_slug: str = Field(description="该会话使用的 model slug。")
    active_turn_id: str | None = Field(
        default=None,
        description="当前活动 Turn ID；同一 Session 最多一个。",
    )
    problem: dict[str, Any] | None = Field(default=None, description="最近一次失败信息。")
    created_at: datetime = Field(description="创建时间，ISO 8601。")
    updated_at: datetime = Field(description="最后更新时间，ISO 8601。")


class SessionPage(BaseModel):
    items: list[SessionOut] = Field(description="按 created_at、id 倒序排列的 Session。")
    next_cursor: str | None = Field(
        description="存在下一页时返回的不透明游标；调用方必须原样传回。"
    )
    has_more: bool = Field(description="是否还有下一页。")


class TurnSubmit(BaseModel):
    input: str = Field(
        min_length=1,
        max_length=100_000,
        description="提交给 Agent 的非空用户输入，最大 100,000 字符。",
        examples=["检查工作区并修复失败的测试。"],
    )

    model_config = ConfigDict(
        json_schema_extra={"examples": [{"input": "检查工作区并修复失败的测试。"}]}
    )


class TurnOut(BaseModel):
    id: str = Field(description="调用方提供的 Turn ID，也是幂等键。")
    status: Literal["queued", "running", "succeeded", "cancelled", "failed"] = Field(
        description="Turn 状态；succeeded、cancelled 和 failed 为终态。"
    )
    command_id: str | None = Field(
        default=None,
        description="内部 Bridge command ID；业务归属和幂等判断应使用 Turn ID。",
    )
    problem: dict[str, Any] | None = Field(default=None, description="Turn 失败信息。")
    created_at: datetime = Field(description="创建时间，ISO 8601。")
    updated_at: datetime = Field(description="最后更新时间，ISO 8601。")


class EventOut(BaseModel):
    protocol_version: Literal[1] = Field(default=1, description="Manager 事件协议版本。")
    seq: int = Field(description="Session 内递增事件序号；调用方应以此去重。")
    session_id: str = Field(description="事件所属的外部 Session ID。")
    turn_id: str | None = Field(description="事件所属的外部 Turn ID；无法归属时为 null。")
    occurred_at: str = Field(description="事件发生时间，ISO 8601 字符串。")
    type: str = Field(description="事件类型；新增类型时调用方应保持向前兼容。")
    data: dict[str, Any] = Field(description="事件类型对应的载荷。")


class EventPage(BaseModel):
    items: list[EventOut] = Field(description="按 seq 升序排列的事件。")
    next_cursor: str = Field(description="下次请求原样传入 cursor 的不透明游标。")


class AdminModelCreate(BaseModel):
    slug: str = Field(
        pattern=r"^[a-z0-9][a-z0-9-]{0,119}$",
        description="稳定模型别名；创建后不可覆盖。",
        examples=["coding-default"],
    )
    label: str = Field(min_length=1, max_length=160, description="管理界面显示名称。")
    provider_model: str = Field(
        min_length=1,
        max_length=240,
        description="提供商模型名，例如 deepseek/deepseek-chat。",
    )
    api: str = Field(default="openai-completions", description="LiteLLM API 协议类型。")
    secret_ref: str | None = Field(
        default=None,
        description="预留的密钥环境变量引用；Manager 不通过该接口接收明文密钥。",
    )
    context_window: int = Field(default=128_000, ge=1, description="上下文窗口。")
    max_tokens: int = Field(default=16_000, ge=1, description="最大输出 token 数。")
    reasoning: bool = Field(default=True, description="是否支持 reasoning/thinking。")


class AdminPolicyCreate(BaseModel):
    slug: str = Field(
        pattern=r"^[a-z0-9][a-z0-9-]{0,119}$",
        description="稳定 Policy 标识；同 slug 再次发布会自动增加 revision。",
        examples=["consumer-default"],
    )
    label: str = Field(min_length=1, max_length=160, description="管理界面显示名称。")
    model_slugs: list[str] = Field(
        min_length=1,
        description="允许使用的、已发布的 model slug；不能重复。",
    )
    default_model_slug: str = Field(description="必须同时包含在 model_slugs 中。")
    cpu: str = Field(default="2", description="传给 OpenSandbox 的 CPU 规格。")
    memory: str = Field(default="4Gi", description="传给 OpenSandbox 的内存规格。")
    max_active_sessions: int = Field(
        default=4,
        ge=1,
        le=32,
        description="单个 Instance 允许的最大活动 Session 数。",
    )
    max_budget: float = Field(
        default=5.0,
        gt=0,
        description="LiteLLM virtual key 在 budget_duration 内的最大预算。",
    )
    budget_duration: str = Field(
        default="24h",
        description="LiteLLM 预算周期，例如 24h；必须是 LiteLLM 接受的格式。",
    )
    rpm_limit: int = Field(default=30, ge=1, description="每分钟最大模型请求数。")
    tpm_limit: int = Field(default=1_000_000, ge=1, description="每分钟最大 token 数。")
    max_parallel_requests: int = Field(
        default=2,
        ge=1,
        description="LiteLLM virtual key 的最大并行请求数。",
    )
    egress_domains: list[str] = Field(
        default_factory=list,
        description=(
            "Sandbox 额外允许访问的公网域名；不含协议和路径。"
            "当前发布接口尚未完整校验域名格式，发布前必须人工复核。"
        ),
    )
