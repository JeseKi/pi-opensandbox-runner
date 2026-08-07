# Model Catalog 配置

`config/runner-catalog.toml` 是 Manager Model 与 Policy 的写入来源。每个模型包含：

| 字段 | 含义 |
| --- | --- |
| `slug` | Session 使用的稳定模型标识 |
| `label` | 展示名称 |
| `provider_model` | 供应商模型元数据 |
| `api` | Pi 使用的 API 协议 |
| `secret_ref` | 供应商凭据的环境变量名称 |
| `context_window` | 上下文窗口声明 |
| `max_tokens` | 最大输出 token 声明 |
| `reasoning` | 是否支持 reasoning |
| `input` | 输入模态，必须包含 `text` |

```toml
[[models]]
slug = "coding-default"
label = "Coding Default"
provider_model = "deepseek/deepseek-v4-flash"
api = "openai-completions"
secret_ref = "DEEPSEEK_API_KEY"
context_window = 264000
max_tokens = 16000
reasoning = true
input = ["text"]
```

Manager 对有效变更发布不可变 revision；无效的热更新不会覆盖最后一次有效 catalog。首次启动时
catalog 缺失或无效会阻止 Manager ready。

Model Catalog 不会自动创建 LiteLLM 上游路由。`slug` 必须和 `litellm/config.yaml` 中的
`model_name` 保持一致。
