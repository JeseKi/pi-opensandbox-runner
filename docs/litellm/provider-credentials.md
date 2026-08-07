# Provider 凭据管理

Provider API key 只存在于 `.litellm.env` 和 LiteLLM 容器环境：

```dotenv
LITELLM_MASTER_KEY=...
DEEPSEEK_API_KEY=...
OPENAI_API_KEY=...
```

`litellm/config.yaml` 使用 `os.environ/...` 引用它们。不要将 key 直接写入 YAML、catalog、Manager
数据库或 Sandbox 环境。

| 凭据 | 持有者 |
| --- | --- |
| provider API key | LiteLLM |
| LiteLLM master key | LiteLLM、Manager |
| Instance virtual key | Manager、对应 Sandbox |
| Manager service/admin token | 上层可信服务或管理员 |

轮换 provider key 通常只需更新 LiteLLM secret 并重启/重载 LiteLLM；轮换 master key 需要同步
更新 LiteLLM 与 Manager。
