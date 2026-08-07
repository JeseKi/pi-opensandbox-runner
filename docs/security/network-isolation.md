# Sandbox 网络隔离

基础 Compose 使用三个网络：

- `manager-ingress`：Manager 对外入口
- `pi-runner-control`：Manager、OpenSandbox、LiteLLM 和数据库控制通信
- `pi-runner-internal`：Sandbox、OpenSandbox proxy 和 LiteLLM 数据面通信

`pi-runner-control` 是内部网络。Sandbox Bridge 端口不发布到宿主机，OpenSandbox proxy 是其唯一
预期入口。Bridge 同时校验调用方地址，只信任回环和配置的 proxy host。

生产环境不要发布 LiteLLM、OpenSandbox 或 PostgreSQL 的端口，也不要把 Sandbox 加入不受控的
公共 Docker 网络。
