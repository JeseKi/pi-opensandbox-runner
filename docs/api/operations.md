# Operation API

```http
GET /v1/operations/{operation_id}
```

Operation 的 `status` 为 `pending`、`running`、`succeeded` 或 `failed`。调用方应轮询到终态；
`phase` 只用于进度展示和诊断，不应固化为业务枚举。

```bash
curl -fsS "$MANAGER_URL/v1/operations/$OPERATION_ID" -H "$AUTH" | jq
```

Operation kind 包括 `provision`、`recovery`、`stop` 和 `destroy`。失败时读取 `problem.code`、
`problem.component` 和 `problem.retryable`，不要解析自然语言 `detail`。
