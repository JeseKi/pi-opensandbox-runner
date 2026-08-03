# Bridge API（内部数据面）

> 本文记录运行在单个 sandbox 内的 Pi Bridge 协议。Runner Manager 会封装这些接口；
> `agent-runner` 和其他业务系统必须使用 [Manager API](manager-api.md)，不得保存 Bridge URL
> 或 proxy token。本文只用于 Bridge 开发、协议维护和故障排查。

经 OpenSandbox server proxy 调用时，所有 `/v1` Bridge 请求都要带：

```http
Authorization: Bearer <bridge-proxy-token>
```

API 文档可直接访问 `${BRIDGE_URL}/docs`。Swagger UI 使用相对 URL 加载 `openapi.json`，所以
无论 OpenSandbox 将它挂在何种代理前缀下，都不会错误请求站点根目录的 `/openapi.json`。文档
页面和 schema 本身不含密钥；实际 `/v1` 调用仍必须在 Swagger UI 的 **Authorize** 中填写
Bearer token。

下面假定：

```bash
BRIDGE_URL="$(jq -r .bridge_url .runtime/alice.json)"
BRIDGE_PROXY_TOKEN="$(jq -r .bridge_proxy_token .runtime/alice.json)"
AUTH="Authorization: Bearer ${BRIDGE_PROXY_TOKEN}"
```

读取 token 的命令同样可能被终端审计或日志系统记录；生产集成应由进程直接读取状态文件，不要
把 token 写入日志。代理鉴权和网络暴露方式见[网络与安全](network-security.md)。

## 创建和列出 Session

使用容器默认 LiteLLM 模型：

```bash
curl -sS -X POST "${BRIDGE_URL}/v1/sessions" \
  -H "$AUTH" -H 'Content-Type: application/json' \
  -d '{"name":"task-one"}' | jq
```

也可以为 Session 覆盖模型并共享一个工作目录：

```bash
curl -sS -X POST "${BRIDGE_URL}/v1/sessions" \
  -H "$AUTH" -H 'Content-Type: application/json' \
  -d '{
    "name":"shared-task",
    "model":"coding-default",
    "cwd":"/root/workspace/shared"
  }' | jq
```

创建时可以指定 Session 专属的 system prompt。默认 `append` 会保留 Pi 内置的 coding-agent
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
完整 Agent 行为约束时使用。Session 的完整 system prompt 会随创建、详情和列表响应返回；
调用方不应把其中的敏感内容写入日志。

列出历史 Session：

```bash
curl -sS "${BRIDGE_URL}/v1/sessions?limit=50" -H "$AUTH" | jq
```

返回的 `next_cursor` 可用于下一页。逻辑 Session 在第一条 assistant 消息产生之前也会写入
SQLite，因此“空 Session”同样可以跨 Bridge 重启保留。

## 发送 Prompt

```bash
curl -sS -X POST "${BRIDGE_URL}/v1/sessions/${SESSION_ID}/prompts" \
  -H "$AUTH" -H 'Content-Type: application/json' \
  -d '{"message":"检查当前项目并解释测试失败原因","delivery":"auto"}' | jq
```

请求返回 `202 Accepted`。`delivery=auto` 在 Pi 空闲时使用 `prompt`，在 Pi 正在生成时使用
`follow_up`。也可显式使用 `steer` 或 `follow_up`；若当前没有活动 turn，会返回 `409`。

可以在发送时切换本次及后续 Session 使用的 LiteLLM 模型与思考强度；`thinking_level` 可单独
指定。切换会先通过 Pi RPC 生效，再发送 Prompt，并写入 Session 元数据，因此停止后恢复
Session 时仍会沿用该设置。

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

## 停止与中止

```bash
curl -sS -X POST "${BRIDGE_URL}/v1/sessions/${SESSION_ID}/abort" -H "$AUTH"
curl -sS -X POST "${BRIDGE_URL}/v1/sessions/${SESSION_ID}/stop" -H "$AUTH"
```

`stop` 只停止 Pi RPC 子进程，不删除 Session。下次发送 Prompt 会从 JSONL 文件自动恢复。
Bridge 默认最多保留 4 个 Pi 进程，空闲 5 分钟后回收；历史 Session 数量不受此限制。

## 文件浏览与命令执行

这些接口同样使用 Bridge proxy Bearer token，内部经由 OpenSandbox Execd 调用，不会公开
Execd 端口。路径不受 workspace 限制，可访问容器内任意 Pi 进程有权访问的路径。

对于面向普通用户的文件页，使用受限的 `/v1/workspace-files` 系列接口。其路径相对
`/root/workspace`，Bridge 拒绝符号链接、绝对路径和父目录跳转：

```bash
curl -sS "${BRIDGE_URL}/v1/workspace-files?path=&depth=1" -H "$AUTH" | jq
curl -sS "${BRIDGE_URL}/v1/workspace-files/content?path=README.md" -H "$AUTH"
```

读取会给出 `Content-Type`、`Content-Disposition`、`ETag`、`X-File-Size` 和
`X-File-Modified-At`。`Content-Disposition: attachment` 是下载建议，前端使用 fetch 读取时
仍可根据 UTF-8、无 NUL、≤1 MiB 的内容决定是否打开编辑器。

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

## 事件 SSE

```bash
curl -N "${BRIDGE_URL}/v1/sessions/${SESSION_ID}/events?cursor=0" -H "$AUTH"
```

每个 SSE `id` 是递增整数。事件数据使用稳定的 Bridge envelope，内部 `event` 原样保留 Pi RPC
事件：

```json
{
  "seq": 12,
  "session_id": "...",
  "timestamp": "...",
  "source": "pi",
  "event": {"type": "message_update"}
}
```

断线后可通过 `cursor=<last-id>` 或 `Last-Event-ID` 恢复。若所需分段日志已被轮转，返回
`410 event_cursor_expired` 和当前最早游标。

## 按 entry cursor 获取上下文

```bash
curl -sS \
  "${BRIDGE_URL}/v1/sessions/${SESSION_ID}/entries?limit=100&cursor=${ENTRY_ID}" \
  -H "$AUTH" | jq
```

这里的 cursor 是 Pi session entry 的 `id`，返回指定 entry 之后的原始 JSONL entry。它不是
token 窗口或字符 offset。调用方可以用 `next_cursor` 继续读取；`leaf_id` 表示当前分支叶节点。
停止状态直接读取持久化 JSONL，运行状态通过 Pi RPC 获取。

## Session CRUD

```bash
curl -sS "${BRIDGE_URL}/v1/sessions/${SESSION_ID}" -H "$AUTH" | jq

curl -sS -X PATCH "${BRIDGE_URL}/v1/sessions/${SESSION_ID}" \
  -H "$AUTH" -H 'Content-Type: application/json' \
  -d '{"name":"new-name"}' | jq

curl -sS -X DELETE "${BRIDGE_URL}/v1/sessions/${SESSION_ID}" -H "$AUTH"
```

当前 PATCH 只允许修改名称。活动 Session 默认拒绝删除；确认后可用
`DELETE ...?force=true`。删除 Session 不会递归删除其 `cwd`，避免误删共享工作目录。

## 编辑 Session system prompt

更新配置在下一条 Prompt 前生效：若 Pi 正处于 idle，Bridge 会在发送下一条 Prompt 前重启该
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

MCP Server 管理与 Session 绑定接口见[外部 MCP](mcp.md)。

返回[文档索引](README.md)。
