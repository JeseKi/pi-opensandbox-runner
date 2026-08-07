# 部署要求

## 主机要求

- Linux 主机和可用的 Docker Engine、Docker Compose plugin
- `bash`、`jq`、`openssl` 和 `uv`
- 能拉取 Python、Node、PostgreSQL、LiteLLM、OpenSandbox Execd 等镜像或软件包
- 足够容纳 Manager DB、LiteLLM DB、OpenSandbox 状态和每个 Instance 两个命名卷的磁盘

## 外部依赖

- 至少一个受 LiteLLM 支持的模型供应商及 API key
- 与 `config/runner-catalog.toml` 模型别名一致的 `litellm/config.yaml` 路由
- 生产环境中供 Manager 使用的 TLS 入口或可信内部网络

## 安全准备

分别生成 OpenSandbox API key、LiteLLM master key、Manager Fernet 加密 key、service token 和
admin token。service/admin token 必须不同，所有密钥文件权限应为 `0600`。
