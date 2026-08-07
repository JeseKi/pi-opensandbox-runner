# API 错误处理

错误使用 Problem JSON：

```json
{
  "type": "https://runner-manager.local/problems/instance_not_ready",
  "title": "instance not ready",
  "status": 409,
  "detail": "runner instance is provisioning",
  "instance": "/v1/instances/user-1/sessions/session-1",
  "code": "instance_not_ready",
  "request_id": "...",
  "component": "runner-manager",
  "retryable": true
}
```

程序判断顺序：

1. HTTP status
2. 稳定的 `code`
3. `retryable`
4. 请求是否幂等

`title` 和 `detail` 只供人阅读，不要解析。记录响应中的 `X-Request-ID`/`request_id`，排障时与
Manager 日志关联。对 `429`、`502`、`503` 等可重试错误使用带抖动的指数退避。
