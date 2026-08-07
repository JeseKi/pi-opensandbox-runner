# 生产环境部署

生产环境至少应完成以下事项：

- 仅通过受控入口暴露 Manager，启用 TLS；Web Terminal 使用 `wss://`。
- 不加载 `compose.dev.yaml`，不公开 OpenSandbox、LiteLLM 和 PostgreSQL 端口。
- 使用秘密管理系统注入所有 key；不要把真实值写入镜像或仓库。
- 对 Manager DB、LiteLLM DB、OpenSandbox 状态和命名卷制定一致的备份策略。
- 为每个 Policy 设置明确的模型、预算、限流、资源和 egress allowlist。
- 使用不可变版本标签发布 Runner、egress 和 Manager 镜像。
- 监控 `/readyz`、Operation 失败、磁盘、PostgreSQL 和模型供应商错误。
- 为 `RUNNER_MANAGER_TERMINAL_ALLOWED_ORIGINS` 配置精确 Origin，不使用通配符。

基础 Compose 适合单机内部部署。需要多副本 Manager 或高可用数据库前，应先完成 PostgreSQL
兼容、Operation worker 竞争控制和共享入口验证；当前默认实现以 SQLite 单实例为基线。
