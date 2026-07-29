# Manager API

Manager API 是 `agent-runner` 等内部 consumer 的稳定接入边界。默认监听
`http://127.0.0.1:8090`，OpenAPI 位于 `/v1/openapi.json`，Swagger 位于 `/v1/docs`。
Swagger 已内置中文的调用流程、鉴权、字段、限制、状态和错误说明；调试时可点击右上角
**Authorize**，输入 token 本身，页面会自动添加 `Bearer` 前缀。

除公开的 `/v1/docs` 和 `/v1/openapi.json` 外，所有业务 `/v1/...` 请求使用 consumer
service token：

```http
Authorization: Bearer <RUNNER_MANAGER_BOOTSTRAP_SERVICE_TOKEN>
```

响应包含 `X-Request-ID` 和 `Runner-Protocol-Version: 1`。错误使用统一 problem：

```json
{
  "type": "https://runner-manager.local/problems/instance_not_ready",
  "title": "instance not ready",
  "status": 409,
  "detail": "runner instance is provisioning:creating_sandbox",
  "instance": "/v1/instances/user-1/sessions/example",
  "code": "instance_not_ready",
  "request_id": "8f...",
  "component": "runner-manager",
  "retryable": true
}
```

调用方应根据 `retryable` 和 HTTP 状态决定重试，不要解析 `detail`。

## Catalog

```http
GET /v1/catalog/models
GET /v1/catalog/policies
```

业务系统只保存 Policy slug。Policy 的模型、预算、速率、资源和 egress 规则由 Manager 管理。

## 确保 Instance

分页列出当前 consumer 的 Instance：

```bash
curl -sS "$MANAGER_URL/v1/instances?limit=100&state=ready" \
  -H "$AUTH" | jq
```

结果按 `created_at DESC, id DESC` 排序。后续请求原样传回 `next_cursor`，并保持
`state`、`policy_slug` 等筛选条件不变。service token 只能看到所属 consumer 的 Instance；
游标是不透明实现细节，调用方不能解析或拼接。

```bash
curl -sS -X PUT "$MANAGER_URL/v1/instances/user-1" \
  -H "$AUTH" -H 'Content-Type: application/json' \
  -d '{"policy_slug":"consumer-default"}' | jq
```

返回 `202`。下面是完整字段结构，时间和 ID 仅为示例：

```json
{
  "instance": {
    "id": "5af...",
    "subject_ref": "user-1",
    "policy_slug": "consumer-default",
    "policy_revision": 1,
    "state": "provisioning",
    "phase": "queued",
    "problem": null,
    "created_at": "2026-07-29T10:00:00Z",
    "updated_at": "2026-07-29T10:00:00Z",
    "ready_at": null
  },
  "operation": {
    "id": "...",
    "kind": "provision",
    "status": "pending",
    "phase": "queued",
    "attempt": 0,
    "problem": null,
    "created_at": "2026-07-29T10:00:00Z",
    "updated_at": "2026-07-29T10:00:00Z",
    "finished_at": null
  }
}
```

轮询：

```http
GET /v1/operations/{operation_id}
GET /v1/instances/{subject_ref}
```

Operation 进入 `succeeded` 或 `failed` 后停止轮询。只有 Instance 为 `ready` 才能创建或访问
Session。ensure 对 `(consumer, subject_ref)` 幂等。

运维动作：

```http
POST /v1/instances/{subject_ref}:reconcile
POST /v1/instances/{subject_ref}:stop
POST /v1/instances/{subject_ref}:destroy
X-Confirm-Destroy: {subject_ref}
```

这些接口同样返回 `202 AcceptedOperation`。当前 `destroy` 不回收命名持久卷。

## Session

Session ID 由 consumer 生成，并在目标 Instance 内唯一：

```bash
curl -sS \
  "$MANAGER_URL/v1/instances/user-1/sessions?limit=100&state=ready" \
  -H "$AUTH" | jq
```

Session 列表使用不透明 cursor 分页，并可按 `state`、`model_slug` 精确筛选。列表只读取
Manager 数据库快照，不逐个调用 Bridge，因此状态可能短暂滞后；需要准确状态时使用下面的单
Session GET。即使 Instance 已 stopped、destroyed 或 failed，仍可读取尚未删除的历史绑定。

```bash
curl -sS -X PUT \
  "$MANAGER_URL/v1/instances/user-1/sessions/$SESSION_ID" \
  -H "$AUTH" -H 'Content-Type: application/json' \
  -d '{
    "title":"首次会话",
    "model_slug":"coding-default"
  }' | jq
```

相关接口：

```http
PUT    /v1/instances/{subject_ref}/sessions/{session_id}
GET    /v1/instances/{subject_ref}/sessions/{session_id}
DELETE /v1/instances/{subject_ref}/sessions/{session_id}
```

`legacy_bridge_session_id` 和 `legacy_cwd` 仅用于旧数据迁移，新 consumer 不应设置。

## Turn

同一 Session 只能有一个活动 Turn。consumer 必须先在自己的数据库中持久化并排队，再调用：

```bash
curl -sS -X PUT \
  "$MANAGER_URL/v1/instances/user-1/sessions/$SESSION_ID/turns/$TURN_ID" \
  -H "$AUTH" -H 'Content-Type: application/json' \
  -d '{"input":"检查工作区并修复测试"}' | jq
```

接口：

```http
PUT  /v1/instances/{subject_ref}/sessions/{session_id}/turns/{turn_id}
GET  /v1/instances/{subject_ref}/sessions/{session_id}/turns/{turn_id}
POST /v1/instances/{subject_ref}/sessions/{session_id}/turns/{turn_id}:cancel
```

Turn ID 是幂等键。相同 ID 与相同输入可安全重试；相同 ID 与不同输入返回
`409 idempotency_conflict`。存在其他活动 Turn 时返回 `409 turn_active`。

## 事件

```http
GET /v1/instances/{subject_ref}/sessions/{session_id}/events?cursor=0&limit=100
```

返回分页事件：

```json
{
  "items": [{
    "protocol_version": 1,
    "seq": 1,
    "session_id": "...",
    "turn_id": "...",
    "occurred_at": "...",
    "type": "agent_end",
    "data": {}
  }],
  "next_cursor": "1"
}
```

使用 `next_cursor` 继续读取。consumer 应按 `seq` 去重，以 `turn_id` 归属事件，不应依赖 Bridge
原始 command ID。

## Workspace 与命令

Manager workspace/command proxy 目前是内部预览能力：

```http
GET|PUT|DELETE /v1/instances/{subject_ref}/sessions/{session_id}/workspace/{path}
POST           /v1/instances/{subject_ref}/sessions/{session_id}/commands
GET|DELETE     /v1/instances/{subject_ref}/sessions/{session_id}/commands/{command_id}
```

- `GET .../workspace/` 列出 Session cwd；带非空 path 的 GET 读取完整文件内容。
- `DELETE` 必须携带完整读取返回的 `If-Match`，否则 Bridge 返回 `428`。
- PUT 只接受 `text/plain` UTF-8，并会向 Bridge 转发 `Content-Type` 和 `If-Match`。
- Range、offset/limit、上传和递归目录深度尚未由 Manager API 暴露。
- 命令强制后台执行，cwd 由 Manager 覆盖。

路径前缀不是强安全边界，不能把 workspace/command API 直接暴露给不可信调用方。

## WebTerminal

Manager 可以为 ready Instance 创建独立 PTY，并向已经完成最终用户授权的外部后端签发
短期、一次性、绑定精确 Origin 的浏览器连接票据：

```http
POST   /v1/instances/{subject_ref}/terminals
GET    /v1/instances/{subject_ref}/terminals
GET    /v1/instances/{subject_ref}/terminals/{terminal_id}
DELETE /v1/instances/{subject_ref}/terminals/{terminal_id}
POST   /v1/instances/{subject_ref}/terminals/{terminal_id}/tickets
WS     /v1/terminal-connections
```

这些 REST API 需要 `terminals:access` scope。浏览器只连接最后一个 WebSocket 路径，并通过
Sec-WebSocket-Protocol 提交 ticket；不能把 service token 交给浏览器。Terminal 与 Agent
Turn 是共享文件系统的独立进程，可并发运行，但不会附着到 Agent stdin。

协议、断线 replay、反向代理配置和可直接运行的 xterm.js 示例见
[WebTerminal API](web-terminal.md)。

## Admin API

管理员 token 只用于内部配置：

```http
POST /admin/v1/models
POST /admin/v1/policies
```

Policy 更新会创建同 slug 的新 revision。不要把 admin token 配置到 `agent-runner`。
创建 Model 只写入 Manager catalog，不会同步 LiteLLM 路由；发布前必须先配置并验证同名
LiteLLM alias。

返回[文档索引](README.md)。
