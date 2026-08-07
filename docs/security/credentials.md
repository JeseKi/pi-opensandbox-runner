# 凭据与 Token

| 凭据 | 用途 | 不应暴露给 |
| --- | --- | --- |
| Manager service token | consumer 业务 API | 浏览器、Sandbox |
| Manager admin token | MCP 管理 | consumer、浏览器、Sandbox |
| OpenSandbox API key | Sandbox 管理 | consumer、Sandbox |
| LiteLLM master key | key/MCP 管理 | consumer、Sandbox |
| Bridge proxy token | 单个 Bridge endpoint | consumer、浏览器 |
| LiteLLM virtual key | 单个 Instance 模型/MCP | 其他 Instance、consumer |
| Terminal ticket | 一次 WebSocket 连接 | 日志、持久存储 |

Manager 使用 `RUNNER_MANAGER_CREDENTIAL_ENCRYPTION_KEY` 加密数据库中的 Bridge token 和 virtual
key。当前没有在线 re-encrypt 工具，不能直接替换 Fernet key。

所有 token 禁止写入日志、URL query、shell history、前端存储和版本库。
