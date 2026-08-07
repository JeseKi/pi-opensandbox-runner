# 文档

本项目同时包含 Runner Manager 控制面和运行于 sandbox 内的 Pi Bridge 数据面。阅读时先确认
自己正在操作哪一层。

- [架构设计](architecture.md)：控制面/数据面边界、实体、持久化和 Turn 流程。
- [Manager API](manager-api.md)：`agent-runner` 等内部 consumer 使用的稳定 API。
- [WebTerminal API](web-terminal.md)：外部 UI 的一次性连接票据、WebSocket 协议和 xterm.js 示例。
- [Manager CLI](cli.md)：Model、Policy 和 Instance 生命周期命令参考。
- [Bridge API](api.md)：Manager 内部调用的数据面协议，仅用于开发和故障排查。
- [运行与维护](operations.md)：部署、策略、Operation、SQLite 备份和故障处理。
- [网络与安全](network-security.md)：token、凭据、网络和文件权限边界。
- [本地开发与集成](development.md)：本地运行、测试以及与 `agent-runner` 联调。
- [外部 MCP](mcp.md)：由 Manager 与 LiteLLM Gateway 管理的 MCP Server 与 Policy 授权。

返回[项目 README](../README.md)。
