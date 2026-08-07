# CLI 故障排查

| 现象 | 原因 | 处理 |
| --- | --- | --- |
| `401 missing_token` | 未提供 token | 检查全局 `--token` 参数位置 |
| `401 invalid_token` | token 无效或已停用 | 检查环境与 token 值 |
| `403 consumer_required` | 使用了 admin token | 改用 consumer service token |
| `404` | 当前 consumer 下无此 subject | 检查 token 所属 consumer 和 subject |
| `409` | Instance 状态不允许该动作 | 查看 Problem `code` 和 `detail` |
| Python traceback | 网络、DNS、TLS 或 JSON 解析异常 | 检查 `--base-url` 和 Manager 日志 |

CLI 不会自动重试。对异步操作的重试应以 API 幂等语义和 `retryable` 字段为依据。
