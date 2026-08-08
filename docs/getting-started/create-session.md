# 创建 Session

## Session 是什么？

Session 是一个 Runner Instance 内的一段持续 Agent 对话。Instance 提供隔离的运行环境；Session
记录这段对话使用的模型和初始工作目录；随后发送给 Agent 的每一条输入称为 Turn。

```text
Runner Instance（例如 user-1 的运行环境）
└── Session（例如“修复网站首页”）
    ├── Turn：检查项目结构
    └── Turn：修复首页样式
```

同一个 Instance 可以有多个 Session。创建 Session 不会新建 Sandbox，也不会立即运行 Agent；它只是
在已有的 Instance 中建立一段可供后续 Turn 使用的对话。

## 执行前：设置变量

先确保上一节创建的 Instance 已为 `ready`，再在当前终端设置以下变量：

```bash
export MANAGER_URL=__MANAGER_ORIGIN__
export MANAGER_TOKEN='<.manager.env 中的 RUNNER_MANAGER_BOOTSTRAP_SERVICE_TOKEN>'
export SUBJECT_REF='user-1'
export AUTH="Authorization: Bearer $MANAGER_TOKEN"
export SESSION_ID='session-1'
```

| 变量 | 来源 | 控制什么 |
| --- | --- | --- |
| `MANAGER_URL` | Manager 对外地址 | 请求发送给哪个 Manager |
| `MANAGER_TOKEN` | 本地开发时为 `.manager.env` 的 service token | 以哪个 consumer 身份调用 |
| `SUBJECT_REF` | 业务系统中的稳定用户或租户 ID | 在哪个 Instance 中创建 Session |
| `SESSION_ID` | 业务系统生成的稳定对话 ID | 创建或复用哪段 Session；只需在该 Instance 内唯一 |
| `AUTH` | 由 `MANAGER_TOKEN` 生成 | HTTP 鉴权请求头 |

`SESSION_ID` 应在网络重试时保持不变。例如客户端未收到响应时，使用同一个 `session-1` 和相同请求
再次调用，Manager 会复用既有 Session，而不会创建第二段对话。

## 1. 使用默认工作目录创建 Session

以下命令创建或复用 `session-1`，并使用 `coding-default` 模型：

```bash
curl -fsS -X PUT \
  "$MANAGER_URL/v1/instances/$SUBJECT_REF/sessions/$SESSION_ID" \
  -H "$AUTH" \
  -H 'Content-Type: application/json' \
  -d '{
    "title": "第一次 Agent 会话",
    "model_slug": "coding-default"
  }' | jq
```

这个接口会同步等待 Bridge 建立 Pi Session，但不会执行 Agent。成功时通常返回：

```json
{
  "id": "session-1",
  "state": "ready",
  "title": "第一次 Agent 会话",
  "model_slug": "coding-default",
  "cwd": "/root/workspace/sessions/session-1",
  "active_turn_id": null,
  "problem": null,
  "created_at": "2026-08-08T06:40:00Z",
  "updated_at": "2026-08-08T06:40:00Z"
}
```

- `state: ready`：可以提交第一条 Turn。
- `cwd`：Agent 的初始工作目录。省略请求中的 `cwd` 时，默认是
  `/root/workspace/sessions/{SESSION_ID}`。
- `active_turn_id: null`：当前没有正在执行的 Agent 请求；同一 Session 同时最多只能有一个。
- `model_slug`：本次 Session 固定使用的模型别名，必须被 Instance 当前 Policy 允许。

## 2. 使用指定工作目录（可选）

如果希望 Agent 从已有项目目录开始，创建另一个 Session 时显式传入容器内绝对路径：

```bash
export SESSION_ID='fix-homepage'

curl -fsS -X PUT \
  "$MANAGER_URL/v1/instances/$SUBJECT_REF/sessions/$SESSION_ID" \
  -H "$AUTH" \
  -H 'Content-Type: application/json' \
  -d '{
    "title": "修复网站首页",
    "model_slug": "coding-default",
    "cwd": "/root/workspace/projects/example"
  }' | jq '{id, state, model_slug, cwd, problem}'
```

响应中会保留该目录：

```json
{
  "id": "fix-homepage",
  "state": "ready",
  "model_slug": "coding-default",
  "cwd": "/root/workspace/projects/example",
  "problem": null
}
```

`cwd` 必须是容器内的绝对路径。它只决定 Agent 的初始目录，不是文件访问权限边界。

下一步：[与 Agent 交互](interact-with-agent.md)。
