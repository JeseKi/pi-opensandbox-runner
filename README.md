# Pi OpenSandbox Runner

在一个 OpenSandbox 管理的独立 Docker 容器中运行
[Pi coding agent](https://github.com/earendil-works/pi)，并通过 HTTP 管理长期存在的
Pi session。

这个项目补的是 Pi RPC 与外部 HTTP 之间的桥，而不是 OpenSandbox execd 的替代品：

- OpenSandbox 管理容器生命周期、端口、execd、持久卷和可选的出站网络策略。
- 容器内的 FastAPI bridge 管理多个 `pi --mode rpc` 子进程。
- SQLite 保存逻辑 session 目录；Pi JSONL 保存真实对话历史。
- 分段 NDJSON 日志保存可恢复的 SSE 事件游标。

## 重要的权限语义

`cwd` 只是 Pi 的初始工作目录，不是权限边界。默认目录是
`/root/workspace/<session-id>`，也可以在创建 session 时指定任意容器内绝对路径。
Pi 以 root 运行，可以直接读写容器内其他目录。

这不会让 Pi 访问宿主机或其他用户的容器；真正的隔离边界是 Docker/OpenSandbox
容器。不要向 sandbox 挂载不希望 Agent 访问的宿主机路径。当前 OpenSandbox Docker
运行时还会丢弃高风险 capability 并启用 `no_new_privileges`。

只有 `/root/.pi` 和 `/root/workspace` 默认位于持久卷中。若 session 使用其他目录，
目录内容可在当前容器内读写，但容器删除后不会自动持久化。

## 快速启动

要求：Linux Docker、Docker Compose、`bash`、`curl`、`jq`、`openssl`。

先从示例准备模型供应商环境变量：

```bash
cp .env.example .env
```

编辑 `.env`，只取消所用供应商变量的注释并填写密钥。然后启动一个名为 `alice` 的完整
实例：

```bash
./scripts/up.sh alice \
  --env-file ./.env \
  --provider anthropic \
  --model claude-sonnet-4-20250514
```

构建时默认使用 `MIRROR_MODE=auto`：若可访问 Google 则使用官方 Debian、PyPI 和 npm
源，否则切换到清华 Debian/PyPI 镜像及 npmmirror。也可以显式指定，避免自动探测带来的
不确定性：

```bash
./scripts/up.sh alice \
  --mirror-mode cn \
  --env-file ./.env \
  --provider anthropic \
  --model claude-sonnet-4-20250514
```

可选值为 `auto`、`cn`、`global`，也可通过宿主机 `MIRROR_MODE` 环境变量设置默认值。
选中的 npm、pip、uv 源会写入最终镜像，因此容器内的 Agent 后续运行这些安装器时仍会
使用相同镜像配置。

脚本会：

1. 启动本地 OpenSandbox Server；
2. 构建 Pi bridge 镜像；
3. 创建两个命名卷；
4. 通过 OpenSandbox API 创建 sandbox；
5. 输出 bridge URL，并将独立 Bearer token 写入权限为 `0600` 的状态文件。

`alice` 是逻辑名称，不强依赖 Docker 自动生成的容器名。运行信息和 token 保存在
`.runtime/alice.json`，权限为 `0600`。默认不会把 token 输出到终端，避免泄漏到 shell
历史、终端采集或 CI 日志。确实需要显示时，显式传入 `--show-token`：

```bash
./scripts/up.sh alice \
  --show-token \
  --env-file ./.env \
  --provider anthropic \
  --model claude-sonnet-4-20250514
```

常用生命周期命令：

```bash
./scripts/status.sh alice
./scripts/down.sh alice
./scripts/up.sh alice --env-file ./.env \
  --provider anthropic --model claude-sonnet-4-20250514
```

`down.sh` 删除 sandbox，但保留 Pi 历史和 workspace 命名卷。再次 `up.sh` 会恢复它们。
永久删除数据需要显式确认：

```bash
./scripts/destroy.sh alice --yes
```

## HTTP API

除 `/healthz` 与 `/readyz` 外，所有 bridge 请求都要带：

```http
Authorization: Bearer <bridge-token>
```

API 文档可直接访问 `${BRIDGE_URL}/docs`。Swagger UI 使用相对 URL 加载
`openapi.json`，所以无论 OpenSandbox 将它挂在何种代理前缀下（例如
`http://127.0.0.1:8080/v1/sandboxes/<id>/proxy/8765/docs`），都不会错误请求站点根目录的
`/openapi.json`。文档页面和 schema 本身不含密钥；实际 `/v1` 调用仍必须在 Swagger UI 的
**Authorize** 中填写 Bearer token。

默认只通过 OpenSandbox 的 server proxy 提供 bridge：compose 将该 server 固定发布到
`127.0.0.1:8080`。项目内的 `Dockerfile.opensandbox` 还将 OpenSandbox Docker runtime
自动分配的 execd/egress 端口强制绑定到 `127.0.0.1`，不会在 `0.0.0.0` 发布随机端口。若要让其他内网机器访问，
请在宿主机上单独配置有认证的反向代理、VPN 或 SSH tunnel；不要直接改为 Docker 全接口监听。

该本地派生镜像还会保留经 server proxy 进入 sandbox 的 `Authorization` 请求头；这是 bridge
Bearer 鉴权及 Swagger UI 的 **Authorize** 功能所必需的。OpenSandbox 的管理 API key 仍不会转发。

下面假定：

```bash
BRIDGE_URL="$(jq -r .bridge_url .runtime/alice.json)"
BRIDGE_TOKEN="$(jq -r .bridge_token .runtime/alice.json)"
AUTH="Authorization: Bearer ${BRIDGE_TOKEN}"
```

读取 token 的命令同样可能被终端审计或日志系统记录；生产集成应由进程直接读取状态
文件，不要把 token 写入日志。

### 创建和列出 session

使用容器默认 provider/model：

```bash
curl -sS -X POST "${BRIDGE_URL}/v1/sessions" \
  -H "$AUTH" -H 'Content-Type: application/json' \
  -d '{"name":"task-one"}' | jq
```

也可以为 session 覆盖模型并共享一个工作目录：

```bash
curl -sS -X POST "${BRIDGE_URL}/v1/sessions" \
  -H "$AUTH" -H 'Content-Type: application/json' \
  -d '{
    "name":"shared-task",
    "provider":"anthropic",
    "model":"claude-sonnet-4-20250514",
    "cwd":"/root/workspace/shared"
  }' | jq
```

列出历史 session：

```bash
curl -sS "${BRIDGE_URL}/v1/sessions?limit=50" -H "$AUTH" | jq
```

返回的 `next_cursor` 可用于下一页。逻辑 session 在第一条 assistant 消息产生之前也会
写入 SQLite，因此“空 session”同样可以跨 bridge 重启保留。

### 发送 prompt

```bash
curl -sS -X POST "${BRIDGE_URL}/v1/sessions/${SESSION_ID}/prompts" \
  -H "$AUTH" -H 'Content-Type: application/json' \
  -d '{"message":"检查当前项目并解释测试失败原因","delivery":"auto"}' | jq
```

请求返回 `202 Accepted`。`delivery=auto` 在 Pi 空闲时使用 `prompt`，在 Pi 正在生成时
使用 `follow_up`。也可显式使用 `steer` 或 `follow_up`；若当前没有活动 turn，会返回
`409`。

其他控制命令：

```bash
curl -sS -X POST "${BRIDGE_URL}/v1/sessions/${SESSION_ID}/abort" -H "$AUTH"
curl -sS -X POST "${BRIDGE_URL}/v1/sessions/${SESSION_ID}/stop" -H "$AUTH"
```

`stop` 只停止 Pi RPC 子进程，不删除 session。下次发送 prompt 会从 JSONL 文件自动
恢复。bridge 默认最多保留 4 个 Pi 进程，空闲 5 分钟后回收；历史 session 数量不受此
限制。

### 事件 SSE

```bash
curl -N "${BRIDGE_URL}/v1/sessions/${SESSION_ID}/events?cursor=0" -H "$AUTH"
```

每个 SSE `id` 是递增整数。事件数据使用稳定的 bridge envelope，内部 `event` 原样保留
Pi RPC 事件：

```json
{
  "seq": 12,
  "session_id": "...",
  "timestamp": "...",
  "source": "pi",
  "event": {"type": "message_update"}
}
```

断线后可通过 `cursor=<last-id>` 或 `Last-Event-ID` 恢复。若所需分段日志已被轮转，
返回 `410 event_cursor_expired` 和当前最早游标。

### 按 entry cursor 获取上下文

```bash
curl -sS \
  "${BRIDGE_URL}/v1/sessions/${SESSION_ID}/entries?limit=100&cursor=${ENTRY_ID}" \
  -H "$AUTH" | jq
```

这里的 cursor 是 Pi session entry 的 `id`，返回指定 entry 之后的原始 JSONL entry。
它不是 token 窗口或字符 offset。调用方可以用 `next_cursor` 继续读取；`leaf_id` 表示
当前分支叶节点。停止状态直接读取持久化 JSONL，运行状态通过 Pi RPC 获取。

### Session CRUD

```bash
curl -sS "${BRIDGE_URL}/v1/sessions/${SESSION_ID}" -H "$AUTH" | jq

curl -sS -X PATCH "${BRIDGE_URL}/v1/sessions/${SESSION_ID}" \
  -H "$AUTH" -H 'Content-Type: application/json' \
  -d '{"name":"new-name"}' | jq

curl -sS -X DELETE "${BRIDGE_URL}/v1/sessions/${SESSION_ID}" -H "$AUTH"
```

当前 PATCH 只允许修改名称。活动 session 默认拒绝删除；确认后可用
`DELETE ...?force=true`。删除 session 不会递归删除其 `cwd`，避免误删共享工作目录。

## 网络策略

默认不传 `networkPolicy`，sandbox 可正常访问模型 API 和其他公网 HTTP 服务。可传
OpenSandbox egress policy JSON：

```bash
./scripts/up.sh alice \
  --env-file ./.env \
  --provider anthropic \
  --model claude-sonnet-4-20250514 \
  --network-policy ./network-policy.json
```

策略格式由 OpenSandbox 的 `networkPolicy` API 定义。使用 deny/default-deny 策略时，
记得同时允许模型供应商域名、DNS 及 Agent 实际需要访问的依赖源。

## 本地开发

```bash
uv sync
uv run ruff check .
uv run mypy src
uv run pytest
```

直接运行 bridge：

```bash
export BRIDGE_API_TOKEN=dev-secret
export PI_DEFAULT_PROVIDER=anthropic
export PI_DEFAULT_MODEL=claude-sonnet-4-20250514
uv run pi-opensandbox-runner
```

## 面向 agent-runner 的集成

后续接入 `/home/jese--ki/Projects/dev/agent-runner` 时，推荐把这里视为每用户 sandbox
的数据面：

- agent-runner 保存 OpenSandbox sandbox ID、bridge URL 与 bridge token；
- 用户操作映射到本项目的 session/prompt/events/entries API；
- SSE `seq` 作为断线续传位置；
- OpenSandbox Server 仍是内部控制面，不把其 API key 暴露给最终用户。

当前 Bearer token 是容器级 token：持有者可管理该容器中的全部 Pi session。若未来一个
容器承载多个不互信用户，应在 agent-runner 网关层做用户与 sandbox 的绑定，或改为每
session 授权。
