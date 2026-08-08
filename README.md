# Pi OpenSandbox Runner

Pi OpenSandbox Runner 是内部 Agent Runner Manager。它负责为上层业务系统管理
OpenSandbox sandbox、LiteLLM virtual key、Runner Policy、持久卷，以及容器内的 Pi Bridge。

`agent-runner` 等业务系统只调用 Manager API，不保存 OpenSandbox API key、LiteLLM master
key、sandbox ID、Bridge URL 或 Bridge proxy token。

## 系统边界

```mermaid
flowchart LR
    product[agent-runner<br/>C 端产品层] -->|Manager service token| manager[Runner Manager<br/>控制面]
    manager -->|API key| opensandbox[OpenSandbox]
    manager -->|master key| litellm[LiteLLM]
    manager -->|内部 proxy token| bridge[Pi Bridge<br/>sandbox 数据面]
    bridge --> pi[Pi RPC]
    pi -->|virtual key| litellm
    manager --> managerdb[(Manager SQLite)]
    bridge --> volumes[(Pi / Workspace 持久卷)]
```

- **Manager** 是唯一面向内部业务系统的稳定边界，负责实例、策略、Session/Turn 映射和运维。
- **Bridge** 运行在每个 sandbox 内，负责 Pi RPC、事件日志、文件和命令；不属于 C 端公共 API。
- **OpenSandbox** 管理容器、端口、持久卷和出站网络策略。
- **LiteLLM** 保存供应商密钥，并为每个 Runner Instance 签发受模型、预算和速率限制的 key。
- **Manager SQLite** 保存控制面状态；LiteLLM 自己仍使用 PostgreSQL。

更完整的职责和实体关系见[架构总览](docs/architecture/index.md)。

## 快速启动 Manager

要求：Linux Docker、Docker Compose、`bash`、`jq`、`openssl` 和 `uv`。

先初始化本地配置：

```bash
make init-config
```

它会创建 `.litellm.env`、`.manager.env` 和 OpenSandbox 配置，并生成所需的本地 secret，不会输出
secret 或覆盖已有值。接着仅需在 `.litellm.env` 填写至少一个模型供应商的 API key，例如
`DEEPSEEK_API_KEY`。

构建 Runner 镜像并启动控制面：

```bash
docker build -t pi-opensandbox-runner:local .
docker build -f Dockerfile.egress -t pi-runner-egress:local .
docker compose up -d --build
curl -fsS http://127.0.0.1:8090/readyz | jq
```

基础 `compose.yaml` 只向宿主机发布 Manager；OpenSandbox 和 LiteLLM 仅通过 Docker 网络
访问。需要从宿主机调试这两个依赖时，先启用开发覆盖：

```bash
cp .env.dev.example .env
docker compose up -d --build
```

`.env` 通过 `COMPOSE_FILE` 加载 `compose.dev.yaml`，只额外发布回环地址上的 `8080` 和
`4000`。不要把本地 `.env` 部署到生产环境。

Manager 首次启动会执行 Alembic migration，并根据 `config/runner-catalog.toml` 发布模型和
Policy 快照，再创建 bootstrap consumer。只有配置了非空且互不相同的 bootstrap service/admin
token 时，才会创建对应 token。

## 接入 agent-runner

`agent-runner` 只需要：

```dotenv
RUNNER_MANAGER_BASE_URL=http://127.0.0.1:8090
RUNNER_MANAGER_API_TOKEN=<RUNNER_MANAGER_BOOTSTRAP_SERVICE_TOKEN>
RUNNER_DEFAULT_POLICY_SLUG=consumer-default
```

上述地址适用于宿主机联调。同一份 Compose 网络中的服务使用
`http://manager:8090`；跨 Compose 项目时必须显式连接共享网络并配置可解析的服务名。

实例创建是异步操作。调用方先确保 Instance，轮询 Operation 到终态，再创建 Session 和提交
Turn。完整请求示例、幂等语义和错误格式见 [API 概览](docs/api/index.md)。
外部产品需要提供 sandbox 交互终端时，可接入 Manager 的一次性票据 WebSocket，详见
[Web Terminal API](docs/api/web-terminal.md)；浏览器不需要也不应持有 Manager service token。

## 常用非破坏性运维入口

```bash
uv run pi-runner-manager-cli --token "$MANAGER_SERVICE_TOKEN" status user-1
uv run pi-runner-manager-cli --token "$MANAGER_SERVICE_TOKEN" reconcile user-1
uv run pi-runner-manager-cli --token "$MANAGER_SERVICE_TOKEN" stop user-1
```

`stop` 和当前的 `destroy` 都会删除 sandbox 并吊销 LiteLLM key，但保留命名持久卷。
持久卷的最终回收需要单独的卷管理流程。完整命令、参数、退出状态和错误处理见
[Manager CLI](docs/cli/index.md)，备份、恢复、策略和故障排查见
[Runner 日常运维](docs/operations/index.md)。

## 文档

- [文档首页](docs/index.md)
- [快速开始](docs/getting-started/index.md)
- [架构总览](docs/architecture/index.md)
- [部署与配置](docs/deployment/requirements.md)
- [Manager API](docs/api/index.md)
- [Manager CLI](docs/cli/index.md)
- [LiteLLM](docs/litellm/index.md)
- [安全与运维](docs/operations/index.md)
- [开发者指南](docs/development/index.md)

Manager 启动后，完整文档站位于 `http://127.0.0.1:8090/mkdocs/docs/`。

Manager Swagger 位于 `http://127.0.0.1:8090/v1/docs`。Bridge Swagger 只用于底层调试，不应
作为业务系统接入入口。

## 本地检查

```bash
uv sync
make check
```
