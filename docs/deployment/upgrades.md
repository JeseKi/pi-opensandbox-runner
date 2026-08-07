# 升级与迁移

## 升级顺序

1. 备份 Manager、LiteLLM、OpenSandbox 状态和命名卷。
2. 构建带固定版本标签的新 Runner、Manager 和 egress 镜像。
3. 先在测试 Policy/Instance 验证 Session、Turn、事件和恢复链路。
4. 停止或滚动替换 Manager；启动时自动执行 Alembic migration。
5. 再次 ensure 或 reconcile 代表性 Instance。

不要在没有备份的情况下回滚包含数据库 migration 的版本。更新 Pi、LiteLLM 或 OpenSandbox 时，
同时验证 Bridge RPC、virtual key 和 proxy patch 的兼容性。

catalog revision 是不可变快照。修改 catalog 不要求数据库 migration，但可能导致已有 Instance
在下一次 ensure 时重新 provision。
