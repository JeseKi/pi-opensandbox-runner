# 预算、限流与并发控制

Runner Policy 的以下字段被写入 Instance virtual key：

| Policy 字段 | 作用 |
| --- | --- |
| `max_budget` | 一个预算周期内允许的成本 |
| `budget_duration` | 预算周期，例如 `24h` |
| `rpm_limit` | 每分钟请求数 |
| `tpm_limit` | 每分钟 token 数 |
| `max_parallel_requests` | 同时进行的模型请求数 |

这些是 LiteLLM 模型网关限制，不等同于 `max_active_sessions`。后者由 Bridge 控制活动 Pi Session
数量。

达到限制时，LiteLLM 通常返回 429 或预算相关错误。consumer 是否重试取决于限制类型：短期速率
限制可退避，预算耗尽则需等待新周期或由管理员调整 Policy。
