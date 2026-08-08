# 创建 Runner Instance

## Runner Instance 是什么？

Runner Instance 是一个可持续使用的、隔离的 Agent 运行环境。它归属于一个 consumer，并通过
`subject_ref` 绑定到一个稳定的业务主体，例如一个用户（`user-1`）或一个租户（`tenant-acme`）。

它不是一次 Agent 请求，也不是一段对话。可以这样理解：

| 概念 | 作用 | 示例 |
| --- | --- | --- |
| Runner Instance | 某个业务主体的隔离运行环境，持有 Sandbox、工作区和运行凭据 | `user-1` 的环境 |
| Session | 该环境中的一段持续 Agent 对话 | “修复网站首页” |
| Turn | Session 中发送给 Agent 的一条输入 | “先检查项目结构” |

同一 consumer 内，一个 `subject_ref` 始终对应同一个 Instance。重复调用“确保 Instance”接口会复用
已有 Instance，不会因为网络重试而创建第二个 Sandbox；只有 Policy 变化或实例需要恢复时才会重新
执行配置。

## 执行前：设置本示例使用的变量

先在当前终端设置以下变量，再执行后面的 `curl`。它们不是 Manager 自动创建的 ID，而是客户端连接
Manager 和指定业务主体所需的输入：

```bash
export MANAGER_URL=__MANAGER_ORIGIN__
export MANAGER_TOKEN='<.manager.env 中的 RUNNER_MANAGER_BOOTSTRAP_SERVICE_TOKEN>'
export SUBJECT_REF='user-1'
export AUTH="Authorization: Bearer $MANAGER_TOKEN"
```

| 变量 | 从哪里获得 | 控制什么 |
| --- | --- | --- |
| `MANAGER_URL` | Manager 对外地址；本地 Compose 默认是当前网页所在的地址 | 请求发送给哪个 Manager |
| `MANAGER_TOKEN` | `.manager.env` 中的 service token；生产环境通常由调用方的凭据系统签发或保存 | 以哪个 consumer 身份调用，以及可用权限 |
| `SUBJECT_REF` | 由你的业务系统决定的稳定 ID，例如用户 ID 或租户 ID | 要创建或复用哪个 Runner Instance |
| `AUTH` | 由上面的 `MANAGER_TOKEN` 拼成 | `curl` 使用的 HTTP `Authorization` 请求头 |

`SUBJECT_REF` 会出现在 URL 的 `/instances/$SUBJECT_REF` 中，因此 `user-1` 就表示“为 user-1
这个业务主体确保运行环境”。之后的 `SESSION_ID` 和 `TURN_ID` 分别在创建对话、发送 Agent 消息时才会
使用；它们不参与 Instance 的身份确定。

## 1. 确保 Instance

以下命令为 `user-1` 创建或复用 Instance，并应用 `consumer-default` Policy：

```bash
response="$(curl -fsS -X PUT \
  "$MANAGER_URL/v1/instances/$SUBJECT_REF" \
  -H "$AUTH" \
  -H 'Content-Type: application/json' \
  -d '{"policy_slug":"consumer-default"}')"

printf '%s\n' "$response" | jq
```

首次调用会返回 `202 Accepted`。这表示 Manager 已接收任务，正在后台创建 Sandbox 和运行凭据；
**不表示 Instance 已可供 Agent 使用**。响应形如：

```json
{
  "instance": {
    "id": "b9c339a0-6e10-4fc8-9d2f-326d370e1f71",
    "subject_ref": "user-1",
    "policy_slug": "consumer-default",
    "policy_revision": 1,
    "state": "provisioning",
    "phase": "queued",
    "problem": null,
    "created_at": "2026-08-08T06:30:00Z",
    "updated_at": "2026-08-08T06:30:00Z",
    "ready_at": null
  },
  "operation": {
    "id": "a3e0f0b2-3046-4b46-8cfd-42d14e4f9f2f",
    "kind": "provision",
    "status": "pending",
    "phase": "queued",
    "attempt": 0,
    "problem": null,
    "created_at": "2026-08-08T06:30:00Z",
    "updated_at": "2026-08-08T06:30:00Z",
    "finished_at": null
  }
}
```

需要关注两个字段：`instance.state` 表示运行环境状态，`operation.id` 是等待后台任务完成时要使用的 ID。

## 2. 保存 Operation ID

上一步的响应已保存到 shell 变量 `response`。单独取出其中的 `operation.id`：

```bash
export OPERATION_ID="$(printf '%s' "$response" | jq -r '.operation.id')"
printf '%s\n' "$OPERATION_ID"
```

输出是一个 Operation UUID，例如：

```text
a3e0f0b2-3046-4b46-8cfd-42d14e4f9f2f
```

这个 ID 只代表这次后台创建任务；它不同于 `SUBJECT_REF`。`SUBJECT_REF` 用于定位业务主体的
Instance，`OPERATION_ID` 用于查询这次创建任务的进度。

## 3. 先查询一次进度

先执行一次查询，看看当前进度：

```bash
curl -fsS "$MANAGER_URL/v1/operations/$OPERATION_ID" -H "$AUTH" \
  | jq '{id, kind, status, phase, attempt, problem}'
```

刚提交后常见响应如下：

```json
{
  "id": "a3e0f0b2-3046-4b46-8cfd-42d14e4f9f2f",
  "kind": "provision",
  "status": "running",
  "phase": "creating_sandbox",
  "attempt": 1,
  "problem": null
}
```

- `status` 为 `pending` 或 `running`：仍在准备，请稍后再次查询。
- `status` 为 `succeeded`：准备完成，可以确认 Instance 状态。
- `status` 为 `failed`：准备失败，查看 `problem.code`、`problem.component` 和
  `problem.retryable`，不要依赖自然语言错误文本。

## 4. 自动等待完成（可选）

如果你的程序不想手动重复查询，可使用以下循环。它会持续显示每次查询的响应摘要，直到成功或失败：

```bash
while true; do
  operation="$(curl -fsS \
    "$MANAGER_URL/v1/operations/$OPERATION_ID" \
    -H "$AUTH")"
  printf '%s\n' "$operation" | jq '{status, phase, problem}'
  status="$(printf '%s' "$operation" | jq -r '.status')"
  case "$status" in
    succeeded) break ;;
    failed) exit 1 ;;
  esac
  sleep 1
done
```

## 5. 确认 Instance 已就绪

Operation 成功后，再查询 Instance 本身：

```bash
curl -fsS "$MANAGER_URL/v1/instances/$SUBJECT_REF" -H "$AUTH" \
  | jq '{subject_ref, state, phase, problem}'
```

成功时响应如下：

```json
{
  "subject_ref": "user-1",
  "state": "ready",
  "phase": "ready",
  "problem": null
}
```

只有 `state` 为 `ready` 时才能创建 Session。若仍是 `provisioning`，继续查询 Operation；若为
`failed`，查看 `problem` 排查原因。

下一步：[创建 Session](create-session.md)。
