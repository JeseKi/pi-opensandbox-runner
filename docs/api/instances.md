# Runner Instance API

Runner Instance 是一个业务主体的隔离、可持续 Agent 运行环境，不是一条 Agent 消息或一段对话。
它由当前 consumer 和 `subject_ref` 唯一确定，持有该主体所需的 Sandbox、工作区和运行凭据。一个
Instance 内可以创建多个 Session；每个 Session 又包含多条 Turn。

```http
GET  /v1/instances
PUT  /v1/instances/{subject_ref}
GET  /v1/instances/{subject_ref}
POST /v1/instances/{subject_ref}:reconcile
POST /v1/instances/{subject_ref}:stop
POST /v1/instances/{subject_ref}:destroy
```

确保 Instance：

```bash
curl -fsS -X PUT "$MANAGER_URL/v1/instances/user-1" \
  -H "$AUTH" -H 'Content-Type: application/json' \
  -d '{"policy_slug":"consumer-default"}' | jq
```

首次请求返回 `202 Accepted`，其响应包含 `instance` 快照和需轮询的 `operation`。`202` 仅表示后台
任务已排队，调用方必须等待 `operation.status=succeeded` 且 `instance.state=ready` 后，才能创建
Session。完整的请求、响应和轮询示例见[快速开始：创建 Runner Instance](../getting-started/create-instance.md)。

ensure、reconcile、stop 和 destroy 返回 `202 AcceptedOperation`。destroy 还要求：

```http
X-Confirm-Destroy: user-1
```

只有 `ready` Instance 可以创建 Session 或访问数据面。`subject_ref` 只在当前 consumer 内唯一。
