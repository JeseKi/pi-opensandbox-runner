# 调试 Bridge

Bridge 通常由 Manager 在 Sandbox 内启动。调试时重点观察：

- `/healthz` 与 `/readyz`
- `/docs` 和 `/openapi.json`
- Bridge SQLite 与 event journal
- Pi RPC 进程状态
- Execd 地址与文件/命令响应
- `models.json` 和 `LITELLM_VIRTUAL_KEY`

Bridge 拒绝非回环或非 `BRIDGE_TRUSTED_PROXY_HOST` 来源。通过 OpenSandbox proxy 调试时需要正确的
Bridge proxy Bearer token。

旧的 `scripts/up.sh` 可用于底层 Bridge/OpenSandbox 开发，但它绕过 Manager 数据库，不用于生产
集成验证。
