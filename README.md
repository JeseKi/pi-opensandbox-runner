# Pi OpenSandbox Runner

在一个 OpenSandbox 管理的独立 Docker 容器中运行
[Pi coding agent](https://github.com/earendil-works/pi)，并通过 HTTP 管理长期存在的
Pi session。

这个项目补的是 Pi RPC 与外部 HTTP 之间的桥，而不是 OpenSandbox execd 的替代品：

- OpenSandbox 管理容器生命周期、端口、execd、持久卷和可选的出站网络策略。
- 容器内的 FastAPI bridge 管理多个 `pi --mode rpc` 子进程。
- SQLite 保存逻辑 session 目录；Pi JSONL 保存真实对话历史。
- 分段 NDJSON 日志保存可恢复的 SSE 事件游标。

## 系统架构与提示词流转

### 实体关系

下图同时描述 SQLite 中的逻辑实体和与其一一对应或按名称关联的持久化文件。其中只有
`SESSION_MCP_SERVER` 的两条关系是数据库外键；其余关系由 bridge 的运行时协议或命名卷维护。

```mermaid
erDiagram
    SANDBOX ||--|| BRIDGE_STATE : "挂载 /root/.pi"
    BRIDGE_STATE ||--o{ SESSION : "保存元数据"
    BRIDGE_STATE ||--o{ MCP_SERVER : "保存定义"
    SESSION ||--o{ SESSION_MCP_SERVER : "绑定"
    MCP_SERVER ||--o{ SESSION_MCP_SERVER : "被绑定"
    SESSION ||--o{ EVENT_SEGMENT : "产生事件"
    SESSION ||--o| PI_SESSION_JSONL : "生成历史"
    SANDBOX ||--|| MODEL_CATALOG : "持久化 models.json"
    SANDBOX ||--|| WORKSPACE_VOLUME : "挂载工作目录"
    LITELLM_PROXY ||--o{ VIRTUAL_KEY : "签发"
    VIRTUAL_KEY ||--|| SANDBOX : "限定模型访问范围"

    SESSION {
        string id "主键"
        string name "名称"
        string cwd "初始工作目录"
        string model "LiteLLM 模型别名"
        string thinking_level "思考等级"
        string session_file "Pi 历史文件"
    }
    MCP_SERVER {
        string id "主键"
        string name "唯一名称"
        string transport "传输方式"
        string url "服务地址"
    }
    SESSION_MCP_SERVER {
        string session_id "Session 外键"
        string server_id "MCP Server 外键"
    }
    EVENT_SEGMENT {
        string session_id "所属 Session"
        integer sequence "事件序号"
        string ndjson_path "NDJSON 路径"
    }
    PI_SESSION_JSONL {
        string session_id "所属 Session"
        string path "文件路径"
    }
    MODEL_CATALOG {
        string fingerprint "配置指纹"
        string path "文件路径"
    }
    VIRTUAL_KEY {
        string models "可用模型"
        integer tpm_limit "每分钟令牌上限"
        number max_budget "总预算"
        string expiry "过期时间"
    }
```

### 一次提示词的泳道图

`provider` 在 bridge API 中固定为 LiteLLM，不再是调用方参数；调用方只选择已授权的模型别名。
模型供应商密钥始终停留在 LiteLLM 容器，sandbox 只持有受限的虚拟密钥（virtual key）。

```mermaid
flowchart LR
    subgraph caller[调用方]
        request[POST /v1/sessions/:id/prompts\nBearer bridge proxy token + model]
        sse[读取 SSE / entries]
    end

    subgraph sandbox[OpenSandbox 私网沙箱]
        subgraph bridge[FastAPI Bridge]
            auth[来源校验与模型白名单校验]
            session[读取/更新 Session 元数据]
            restart{模型目录或运行配置\n是否已变更?}
            supervisor[会话监督器\n启动或复用 Pi RPC]
            journal[写入 Event Journal]
        end
        subgraph persistence[持久卷 /root/.pi]
            sqlite[(SQLite：会话与 MCP 绑定)]
            catalog[models.json\n原子替换]
            history[(Pi session JSONL)]
            events[(分段 NDJSON)]
        end
        pi[Pi RPC 子进程\n固定使用 LiteLLM]
    end

    subgraph gateway[LiteLLM 私网网关]
        key[验证沙箱虚拟密钥\n模型、预算、RPM/TPM]
        route[按模型别名路由]
    end

    subgraph upstream[模型供应商]
        model[DeepSeek / OpenAI / Anthropic]
    end

    request --> auth
    auth -->|无效令牌 / 未授权模型| reject[401 / 422]
    auth --> session
    session <--> sqlite
    session --> restart
    catalog -.模型目录指纹.-> restart
    restart -->|是，且未生成| supervisor
    restart -->|正在生成| pending[409：模型目录更新待处理]
    restart -->|否| supervisor
    supervisor --> pi
    pi -->|OpenAI 兼容请求\n虚拟密钥| key
    key -->|限流 / 预算 / 模型限制| gateway_error[LiteLLM 4xx 错误]
    key --> route
    route -->|供应商密钥仅在此处使用| model
    model --> route --> pi
    pi --> history
    pi --> journal --> events
    journal --> sse
```

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

先准备仅供 LiteLLM 网关读取的供应商密钥：

```bash
cp .litellm.env.example .litellm.env
chmod 600 .litellm.env
```

编辑 `.litellm.env` 填写供应商密钥和 LiteLLM master key。该文件不会挂载或注入 sandbox。
默认模型别名 `coding-default` 在 `litellm/config.yaml` 与 `config/pi-models.json` 中定义，
默认路由到 DeepSeek V4 Pro。`gpt-5.6-terra` 已在 LiteLLM 中配置；需要先在目标 sandbox 的
虚拟 Key 中授权，再通过 bridge 模型目录接口加入该 sandbox。

### 管理虚拟 Key 预算

LiteLLM 管理 UI 位于 `http://127.0.0.1:4000/ui`，只发布到宿主机回环地址。使用
`.litellm.env` 中的 `LITELLM_MASTER_KEY` 登录；该 key 只应由受信任的宿主机管理员使用，绝不
注入 sandbox 或写入自动化日志。

每个 sandbox 的 virtual key 初始为 `$5` 预算、`24h` 预算周期，并在 `Asia/Shanghai` 时区的每日
零点自动重置；key 本身不按时间过期。打开 UI 的 Keys 页面后，按
`pi-runner-<sandbox 名称>-...` alias 或 `sandbox_name` metadata 定位对应 key，即可编辑
`Max Budget`、`Budget Duration` 和其他 LiteLLM 限制。这些改动会立即作用于运行中的 sandbox，
无需重启 Pi。

需要提前恢复额度时，在对应 key 的详情页使用 **Reset Spend**，它会把当前周期 spend 置为 `$0`，
但不会更换 key 字符串。不要使用 **Regenerate Key** 或 **Auto-Rotation**：它们会生成新的 key，
而运行中的 sandbox 不会自动取得新 secret。

UI 对单个 key 的改动不会跨 `down.sh` / `up.sh` 保留：旧 key 会被吊销，新 key 会重新使用
`litellm/config.yaml` 中的默认值。要修改未来 sandbox 的默认预算或周期，更新该文件后重启
LiteLLM 容器。已经存在的旧 24 小时到期 key，可在 UI 中设为 Never Expire 并补上预算周期，或执行
`down.sh` 后再 `up.sh` 以签发新 key。

### 热更新 Pi 可选模型

模型目录是每个 sandbox 独立、持久化的配置。先在 LiteLLM UI 给该 sandbox 的 virtual key
授权模型，再读取目录并原子提交更新；bridge 会在下一次安全请求前重启对应 Pi RPC 子进程，
不会重建 sandbox 容器。

```bash
curl -sS "${BRIDGE_URL}/v1/models/config" \
  -H "Authorization: Bearer ${BRIDGE_PROXY_TOKEN}" > models-config.json

# 在 models-config.json 的 providers.litellm.models 中加入 gpt-5.6-terra 元数据。
curl -sS -X PUT "${BRIDGE_URL}/v1/models/config" \
  -H "Authorization: Bearer ${BRIDGE_PROXY_TOKEN}" \
  -H "Content-Type: application/json" \
  --data-binary @models-config.json | jq
```

上传会检查 Pi 文件结构、固定的 LiteLLM 私网配置，以及当前 virtual key 是否已获该模型授权。
上传期间正在生成的 Session 不会中断；它结束前的新请求会返回 `409 model_catalog_update_pending`。
然后启动一个名为 `alice` 的完整实例：

```bash
./scripts/up.sh alice \
  --model coding-default
```

构建时默认使用 `MIRROR_MODE=auto`：若可访问 Google 则使用官方 Debian、PyPI 和 npm
源，否则切换到清华 Debian/PyPI 镜像及 npmmirror。也可以显式指定，避免自动探测带来的
不确定性：

```bash
./scripts/up.sh alice \
  --mirror-mode cn \
  --model coding-default
```

可选值为 `auto`、`cn`、`global`，也可通过宿主机 `MIRROR_MODE` 环境变量设置默认值。
选中的 npm、pip、uv 源会写入最终镜像，因此容器内的 Agent 后续运行这些安装器时仍会
使用相同镜像配置。

脚本会：

1. 启动本地 OpenSandbox Server；
2. 在本地 Pi bridge 镜像不存在时构建镜像；
3. 创建两个命名卷；
4. 通过 OpenSandbox API 创建 sandbox；
5. 输出 bridge URL，并将独立的外部代理 Bearer token 写入权限为 `0600` 的状态文件。

`alice` 是逻辑名称，不强依赖 Docker 自动生成的容器名。运行信息和 token 保存在
`.runtime/alice.json`，权限为 `0600`。默认不会把 token 输出到终端，避免泄漏到 shell
历史、终端采集或 CI 日志。确实需要显示时，显式传入 `--show-token`：

```bash
./scripts/up.sh alice \
  --show-token \
  --model coding-default
```

常用生命周期命令：

```bash
./scripts/status.sh alice
./scripts/down.sh alice
./scripts/up.sh alice --model coding-default
```

`down.sh` 删除 sandbox，但保留 Pi 历史和 workspace 命名卷。再次 `up.sh` 会恢复它们。
永久删除数据需要显式确认：

```bash
./scripts/destroy.sh alice --yes
```

升级到外部 proxy token 模型前创建的 sandbox 仍可暂时使用旧 Bridge 鉴权；要移除其容器内的
旧 bridge token，执行一次 `./scripts/down.sh alice && ./scripts/up.sh alice --model coding-default`。
这会签发新 token 和 LiteLLM virtual key，但不会删除两个持久卷。

## HTTP API

经 OpenSandbox server proxy 调用时，所有 `/v1` bridge 请求都要带：

```http
Authorization: Bearer <bridge-proxy-token>
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

派生的 OpenSandbox server proxy 会按 sandbox metadata 中保存的 SHA-256 摘要校验该 token，
然后在转发前剥离 `Authorization`。因此 token 不会出现在 Pi 容器环境变量、进程环境或 bridge
配置中。Bridge 仅接受本容器回环请求与 `opensandbox` 代理容器的真实 TCP 来源；同一 Docker
私网中的其他 sandbox 不能绕过 proxy 直连。OpenSandbox 的管理 API key 同样不会转发。

下面假定：

```bash
BRIDGE_URL="$(jq -r .bridge_url .runtime/alice.json)"
BRIDGE_PROXY_TOKEN="$(jq -r .bridge_proxy_token .runtime/alice.json)"
AUTH="Authorization: Bearer ${BRIDGE_PROXY_TOKEN}"
```

读取 token 的命令同样可能被终端审计或日志系统记录；生产集成应由进程直接读取状态
文件，不要把 token 写入日志。

### 创建和列出 session

使用容器默认 LiteLLM 模型：

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
    "model":"coding-default",
    "cwd":"/root/workspace/shared"
  }' | jq
```

创建时可以指定 session 专属的 system prompt。默认 `append` 会保留 Pi 内置的 coding-agent
prompt，并在其后追加指令：

```bash
curl -sS -X POST "${BRIDGE_URL}/v1/sessions" \
  -H "$AUTH" -H 'Content-Type: application/json' \
  -d '{
    "name":"review-in-chinese",
    "system_prompt":"始终使用中文；修改代码前先说明计划。",
    "system_prompt_mode":"append"
  }' | jq
```

`system_prompt_mode=replace` 会改用给定文本完全替换 Pi 默认 prompt，仅应在调用方自行提供了
完整 Agent 行为约束时使用。Session 的完整 system prompt 会随创建、详情和列表响应返回；调用方
不应把其中的敏感内容写入日志。

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

可以在发送时切换本次及后续 session 使用的 LiteLLM 模型与思考强度；`thinking_level` 可单独指定。切换会先通过 Pi RPC 生效，再发送 prompt，并
写入 session 元数据，因此停止后恢复 session 时仍会沿用该设置。

```bash
curl -sS -X POST "${BRIDGE_URL}/v1/sessions/${SESSION_ID}/prompts" \
  -H "$AUTH" -H 'Content-Type: application/json' \
  -d '{
    "message":"用新的模型继续分析",
    "model":"coding-default",
    "thinking_level":"off"
  }' | jq
```

具体可用的模型和支持的思考等级取决于 Pi 已配置的 LiteLLM 模型目录；不支持时 Pi 会拒绝请求。

### 外部 MCP

Pi 本身通过内置 Extension 使用 MCP。首期支持远程 Streamable HTTP 与兼容 SSE；不支持
stdio、浏览器 OAuth、MCP resources/prompts/sampling。MCP Server 可在同一容器中复用，再按
Session 绑定。工具会以 `mcp_<server>_<tool>` 注册到 Pi，并和普通 Pi tool call 一样出现在
session entries 与 SSE 事件中。

认证值不经 API 保存。将 `MCP_*` 变量放入独立的 `.sandbox.env`（可从
`.sandbox.env.example` 复制），并在启动时显式传入，例如：

```dotenv
MCP_CONTEXT7_TOKEN=replace-me
```

重建 sandbox 时显式传入该文件：

```bash
./scripts/up.sh alice --mcp-env-file .sandbox.env
```

然后创建一个 MCP Server。Header 模板只能引用 `MCP_*` 变量，因此不会读取 bridge token 或
模型供应商 token，GET 响应也只会返回模板而非展开后的值：

```bash
MCP_SERVER_ID="$(curl -sS -X POST "${BRIDGE_URL}/v1/mcp/servers" \
  -H "$AUTH" -H 'Content-Type: application/json' \
  -d '{
    "name":"context7",
    "transport":"streamable_http",
    "url":"https://mcp.context7.com/mcp",
    "headers":{"Authorization":"Bearer ${MCP_CONTEXT7_TOKEN}"}
  }' | jq -r .id)"

curl -sS -X POST "${BRIDGE_URL}/v1/sessions" \
  -H "$AUTH" -H 'Content-Type: application/json' \
  -d "{\"name\":\"with-context7\",\"mcp_server_ids\":[\"${MCP_SERVER_ID}\"]}" | jq
```

也可以绑定已存在的 Session，`PUT` 是完整替换，空数组表示解绑全部：

```bash
curl -sS -X PUT "${BRIDGE_URL}/v1/sessions/${SESSION_ID}/mcp-servers" \
  -H "$AUTH" -H 'Content-Type: application/json' \
  -d "{\"server_ids\":[\"${MCP_SERVER_ID}\"]}" | jq
```

修改绑定时 Pi 正在生成会返回 `409 session_streaming`；否则新配置在下一条 prompt 前通过重启
idle Pi 生效。更新 Server 定义不会中断运行中的 Pi，所有已绑定 Session 会在下一条非流式
prompt 前使用新快照。若缺少 Header 模板引用的环境变量，prompt 返回
`422 mcp_environment_missing`。正在被绑定的 Server 不能删除，需先解绑。

默认 MCP URL 必须为 HTTPS。仅在可信内网开发服务确实使用 HTTP 时，才在 `.sandbox.env` 设置
`MCP_ALLOW_INSECURE_HTTP=1` 后重建 sandbox。模型供应商域名不应配置给 Pi：模型请求只会经
Docker 私网中的 LiteLLM 转发。

其他控制命令：

```bash
curl -sS -X POST "${BRIDGE_URL}/v1/sessions/${SESSION_ID}/abort" -H "$AUTH"
curl -sS -X POST "${BRIDGE_URL}/v1/sessions/${SESSION_ID}/stop" -H "$AUTH"
```

### 文件浏览与命令执行

这些接口同样使用 bridge proxy Bearer token，内部经由 OpenSandbox Execd 调用，不会公开 Execd
端口。路径不受 workspace 限制，可访问容器内任意 Pi 进程有权访问的路径。

```bash
# 类似 ls：默认只返回直接子项；提高 depth 可递归浏览
curl -sS "${BRIDGE_URL}/v1/files?path=/root/workspace&depth=1" -H "$AUTH" | jq

# 浏览文本文件第 20 至 69 行；不传 offset/limit 则作为二进制文件下载。
# 完整读取的响应包含强 ETag，保存和删除时必须使用它。
curl -sS "${BRIDGE_URL}/v1/files/content?path=/root/workspace/README.md&offset=20&limit=50" \
  -H "$AUTH"

# 上传仅创建新文件。path 可为完整目标文件路径，也可为已存在目录（自动使用原文件名）。
curl -sS -X POST "${BRIDGE_URL}/v1/files/upload" -H "$AUTH" -H 'If-None-Match: *' \
  -F 'path=/root/workspace/' \
  -F 'file=@./input.csv'

# 条件保存：先完整读取并保存 ETag；仅接受 <= 1 MiB、有效 UTF-8、无 NUL 字节的普通文件。
ETAG="$(curl -sS -D - -o /dev/null \
  "${BRIDGE_URL}/v1/files/content?path=/root/workspace/notes.txt" -H "$AUTH" \
  | awk 'BEGIN { IGNORECASE=1 } /^etag:/ { gsub("\\r", "", $2); print $2 }')"
printf '更新后的文本\n' | curl -sS -X PUT \
  "${BRIDGE_URL}/v1/files/content?path=/root/workspace/notes.txt" \
  -H "$AUTH" -H "If-Match: ${ETAG}" -H 'Content-Type: text/plain; charset=utf-8' \
  --data-binary @-

# 删除也必须携带读取到的 ETag；过期版本会返回 412，不会覆盖或删除更新后的文件。
curl -sS -X DELETE "${BRIDGE_URL}/v1/files?path=/root/workspace/input.csv" \
  -H "$AUTH" -H "If-Match: ${ETAG}"

# 执行命令。前台命令返回 Execd 的 SSE 输出；background=true 后可查询状态和日志。
curl -N -X POST "${BRIDGE_URL}/v1/commands" \
  -H "$AUTH" -H 'Content-Type: application/json' \
  -d '{"command":"ls -la /root/workspace","cwd":"/root/workspace","timeout":30000}'
```

后台命令返回的 command ID 可用于：

```bash
curl -sS "${BRIDGE_URL}/v1/commands/${COMMAND_ID}" -H "$AUTH" | jq
curl -sS "${BRIDGE_URL}/v1/commands/${COMMAND_ID}/logs" -H "$AUTH"
curl -X DELETE "${BRIDGE_URL}/v1/commands/${COMMAND_ID}" -H "$AUTH"
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

### 编辑 Session system prompt

更新配置在下一条 prompt 前生效：若 Pi 正处于 idle，bridge 会在发送下一条 prompt 前重启该
Session 的 Pi RPC 进程；若正在生成则返回 `409 session_streaming`，需先等待或 stop。更新和
清除都不会改写 Pi 的历史 JSONL。

```bash
# 完整替换该 Session 的自定义配置；append 是默认模式
curl -sS -X PUT "${BRIDGE_URL}/v1/sessions/${SESSION_ID}/system-prompt" \
  -H "$AUTH" -H 'Content-Type: application/json' \
  -d '{"system_prompt":"所有回答用中文，先运行相关测试。","system_prompt_mode":"append"}' | jq

# 高级模式：替换 Pi 的内置 system prompt
curl -sS -X PUT "${BRIDGE_URL}/v1/sessions/${SESSION_ID}/system-prompt" \
  -H "$AUTH" -H 'Content-Type: application/json' \
  -d '{"system_prompt":"You are a terse code reviewer.","system_prompt_mode":"replace"}' | jq

# 清除自定义配置，恢复 Pi 默认 prompt
curl -sS -X DELETE "${BRIDGE_URL}/v1/sessions/${SESSION_ID}/system-prompt" \
  -H "$AUTH" | jq
```

## 网络策略

Sandbox 与 LiteLLM 处于同一个 Docker 私网，模型供应商 API 密钥不会进入 sandbox；Pi 只能使用
它自己的 LiteLLM virtual key。LiteLLM 管理/调试端口仅发布到宿主机 `127.0.0.1:4000`；sandbox
仍只通过私网访问 `litellm:4000`。每个 sandbox 拿到的 virtual key 仅允许项目模型白名单；默认
预算为每日 $5（Asia/Shanghai 零点重置），停止或销毁时会立即吊销。

OpenSandbox v0.2.2 的 Docker 后端不能在自定义 Docker network 上同时启用 `networkPolicy`。
因此当前版本不在创建请求中提交该策略；若需要强制域名级 egress 白名单，应在宿主机防火墙、专用
egress proxy，或支持该组合的 OpenSandbox 运行时中实施。

## 本地开发

```bash
uv sync
uv run ruff check .
uv run mypy src
uv run pytest
```

直接运行 bridge：

```bash
export PI_DEFAULT_MODEL=coding-default
uv run pi-opensandbox-runner
```

## 面向 agent-runner 的集成

后续接入 `/home/jese--ki/Projects/dev/agent-runner` 时，推荐把这里视为每用户 sandbox
的数据面：

- agent-runner 保存 OpenSandbox sandbox ID、bridge URL 与 bridge proxy token；
- 用户操作映射到本项目的 session/prompt/events/entries API；
- SSE `seq` 作为断线续传位置；
- OpenSandbox Server 仍是内部控制面，不把其 API key 暴露给最终用户。

当前 Bearer token 是容器级 proxy token：持有者可管理该容器中的全部 Pi session。若未来一个
容器承载多个不互信用户，应在 agent-runner 网关层做用户与 sandbox 的绑定，或改为每
session 授权。
