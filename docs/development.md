# 本地开发与集成

## 环境与检查

项目要求 Python 3.13：

```bash
uv sync
make check
```

`make check` 依次运行 Ruff、mypy 和 pytest。

## 运行 Bridge

Bridge 是 sandbox 内的数据面。单独启动仅适合开发其 API、Pi RPC 或事件日志：

```bash
export PI_DEFAULT_MODEL=coding-default
uv run pi-opensandbox-runner
```

直接运行时需要自行提供 Pi、LiteLLM virtual key、工作区和 Bridge 状态目录。业务系统不应使用
这种模式。

## 运行 Manager

Manager 依赖 OpenSandbox 和 LiteLLM。准备 `.litellm.env`、`.manager.env` 与
`.runtime/opensandbox.toml` 后：

```bash
cp .env.dev.example .env
docker build -t pi-opensandbox-runner:local .
docker build -f Dockerfile.egress -t pi-runner-egress:local .
docker compose up -d --build
curl -fsS http://127.0.0.1:8090/readyz
```

开发 `.env` 会同时加载 `compose.yaml` 和 `compose.dev.yaml`，把 OpenSandbox `8080` 与
LiteLLM `4000` 额外发布到宿主机回环地址。基础 Compose 不发布这两个端口。

也可以让依赖运行在 Compose 中，只在宿主机启动 Manager：

```bash
set -a
source .manager.env
set +a
export OPENSANDBOX_BASE_URL=http://127.0.0.1:8080
export LITELLM_BASE_URL=http://127.0.0.1:4000
uv run pi-runner-manager
```

宿主机模式的 SQLite 默认写入 `./data/runner-manager.db`。容器模式使用
`manager-data:/app/data`。

## 与 agent-runner 联调

`agent-runner` 只配置 Manager 地址、service token 和默认 Policy：

```dotenv
RUNNER_MANAGER_BASE_URL=http://127.0.0.1:8090
RUNNER_MANAGER_API_TOKEN=<service token>
RUNNER_DEFAULT_POLICY_SLUG=consumer-default
```

禁止在 `agent-runner` 中配置或持久化：

- `OPENSANDBOX_API_KEY`
- `LITELLM_MASTER_KEY`
- `RUNNER_MANAGER_CREDENTIAL_ENCRYPTION_KEY`
- sandbox ID、Bridge URL、Bridge proxy token
- LiteLLM virtual key

调试时先确认 Manager：

```bash
curl -fsS http://127.0.0.1:8090/readyz | jq
uv run pi-runner-manager-cli --token "$MANAGER_SERVICE_TOKEN" policies
```

然后从 `agent-runner` 创建 Session。正常链路应依次看到 Instance provision Operation、
Manager Session、Turn 以及带正确 `turn_id` 的事件。

## 数据库 migration

Manager 启动时自动执行：

```text
alembic upgrade head
```

migration 位于 `src/pi_opensandbox_manager/migrations/`。需要新增字段时编写新的 Alembic
revision，不要在运行时代码中执行硬编码 `ALTER TABLE` 或 ensure column。

## 低层脚本

`scripts/up.sh`、`down.sh`、`destroy.sh` 和 `egress-policy.sh` 是 Bridge/OpenSandbox 的低层
调试工具。它们会绕过 Manager 创建独立 sandbox，不应作为 `agent-runner` 的集成方式，也不应
与 Manager 管理的同一 subject/volume 混用。

返回[文档索引](README.md)。
