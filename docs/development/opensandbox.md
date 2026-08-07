# 集成 OpenSandbox

Manager 使用 OpenSandbox API 创建、查询和删除 Sandbox，并获取 Bridge endpoint。创建 payload 包含：

- Runner image 与 entrypoint
- CPU、memory 和无限期 Sandbox timeout
- Bridge/LiteLLM/Pi 环境变量
- 默认拒绝的 network policy
- Instance metadata 与 Bridge token hash
- Pi state、Workspace 两个持久卷

OpenSandbox server proxy patch 根据 Sandbox metadata 校验 Bridge Bearer token；network patch 负责将
egress policy 应用到 Docker 环境。升级 OpenSandbox 时必须重新验证这两个 patch。
