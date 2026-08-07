# 集成 Pi

Runner 镜像安装固定版本的 `@earendil-works/pi-coding-agent`，Bridge 通过 RPC 管理 Pi 进程。

集成约束：

- Pi Session history 写入 `/root/.pi/agent/sessions` 持久卷。
- 模型配置由 Bridge 下发到 `/root/.pi/bridge/models.json`。
- 模型请求只访问 LiteLLM，不直接持有 provider key。
- Session prompt、abort、compact、状态和事件由 Bridge 转换成 HTTP 协议。
- MCP 是否启用由 Policy 的 `mcp_server_ids` 决定。

升级 Pi 版本时验证 RPC 事件 schema、Session 恢复、abort、compaction、工具调用和模型配置兼容性。
