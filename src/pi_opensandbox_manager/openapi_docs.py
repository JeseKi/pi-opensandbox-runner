from __future__ import annotations

from typing import Any

from .schemas import ProblemOut

APP_DESCRIPTION = """
Pi Runner Manager 是 `agent-runner` 等内部业务系统访问 Pi/OpenSandbox 的稳定控制面。
它负责 Runner Policy、用户 Instance、Session、Turn、工作区和后台命令，不面向终端用户直接开放。

## 鉴权

- `/v1/**` 使用 **service token**，并按 consumer 隔离数据。
- `/admin/v1/**` 使用 **admin token**，仅供内部平台发布模型和 Policy。
- 点击右上角 **Authorize**，输入 token 本身即可；Swagger 会自动添加 `Bearer` 前缀。
- 不要把 admin token 配置到 `agent-runner`，也不要把 Manager 直接暴露到公网。

## 推荐调用顺序

1. 读取 Catalog，选择已发布的 `policy_slug` 和 `model_slug`。
2. `PUT /v1/instances/{subject_ref}` 幂等确保用户 Instance。
3. 轮询返回的 Operation，直到 `succeeded`；Instance 必须为 `ready`。
4. 幂等创建 Session，再用调用方生成的 Turn ID 提交 Turn。
5. 按 `next_cursor` 增量读取事件；调用方按 `seq` 去重并持久化游标。

## 通用约定

- 所有响应包含 `X-Request-ID` 和 `Runner-Protocol-Version: 1`。
- 错误响应使用 `application/problem+json`。程序应判断 `status`、`code` 和 `retryable`，
  不要解析可能变化的 `detail` 文本。
- `subject_ref`、`session_id`、`turn_id` 都由调用方生成；同一 consumer 内保持稳定且唯一。
- 返回 `202` 表示请求已接收，不表示后台操作已经完成。
"""

OPENAPI_TAGS = [
    {
        "name": "Catalog（目录）",
        "description": "读取业务系统可选择的已发布模型和 Runner Policy。",
    },
    {
        "name": "Instance（运行实例）",
        "description": "管理 consumer 用户对应的 OpenSandbox Runner 实例及异步生命周期。",
    },
    {
        "name": "Session（会话）",
        "description": "在 ready Instance 中幂等创建、查询和删除 Pi 会话。",
    },
    {
        "name": "Turn（任务轮次）",
        "description": "提交 Agent 输入、查询或取消 Turn，以及增量读取运行事件。",
    },
    {
        "name": "Workspace（工作区）",
        "description": "预览能力：代理访问 Session 工作目录中的文件和目录。",
    },
    {
        "name": "Command（后台命令）",
        "description": "预览能力：在 Session 工作目录中执行、查询和中止后台命令。",
    },
    {
        "name": "Terminal（交互终端）",
        "description": "管理 Instance 级 PTY，并签发浏览器一次性 WebSocket 连接票据。",
    },
    {
        "name": "Admin（内部管理）",
        "description": "仅供核心平台使用 admin token 发布模型与 Policy revision。",
    },
]

PROBLEM_RESPONSES: dict[int | str, dict[str, Any]] = {
    401: {"model": ProblemOut, "description": "未提供 Bearer token，或 token 无效。"},
    403: {"model": ProblemOut, "description": "token 类型错误或缺少所需 scope。"},
    404: {"model": ProblemOut, "description": "资源不存在，或不属于当前 consumer。"},
    409: {"model": ProblemOut, "description": "资源当前状态不允许执行该操作。"},
    422: {"model": ProblemOut, "description": "请求字段、幂等键或业务规则校验失败。"},
    502: {"model": ProblemOut, "description": "LiteLLM、OpenSandbox 或 Pi Bridge 上游调用失败。"},
    503: {"model": ProblemOut, "description": "服务或依赖暂时不可用，可结合 retryable 重试。"},
}


def api_doc(
    *,
    summary: str,
    description: str,
    tag: str,
    operation_id: str,
    response_description: str = "请求成功。",
    responses: dict[int | str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    documented_responses = dict(PROBLEM_RESPONSES)
    if responses:
        documented_responses.update(responses)
    return {
        "summary": summary,
        "description": description,
        "tags": [tag],
        "operation_id": operation_id,
        "response_description": response_description,
        "responses": documented_responses,
    }
