# Bridge 内部 API

Bridge 运行在每个 Sandbox 内，只允许 Manager 经 OpenSandbox proxy 访问。它提供：

- Session 创建、读取、更新、删除、历史与 prompt
- 运行状态、abort、compact、事件批量读取与 SSE
- 模型目录读取和下发
- 完整容器文件、受限 workspace 文件
- 命令和 PTY terminal

Bridge 的 Swagger 位于其 endpoint 的 `/docs`，OpenAPI 位于 `/openapi.json`。访问时需要对应
Instance 的 Bridge proxy token。

!!! warning "内部协议"

    consumer 不应保存 Bridge URL、token、Session ID 或 command ID，也不应直接依赖 Bridge
    schema。Manager API 才是产品集成边界。
