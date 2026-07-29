# 外部 MCP

Pi 通过内置 Extension 使用 MCP。当前支持远程 Streamable HTTP 与兼容 SSE；不支持 stdio、
浏览器 OAuth、MCP resources、prompts 或 sampling。

MCP Server 可在同一容器中复用，再按 Session 绑定。工具会以
`mcp_<server>_<tool>` 注册到 Pi，并和普通 Pi tool call 一样出现在 Session entries 与 SSE
事件中。

## 配置认证环境

认证值不经 API 保存。将 `MCP_*` 变量放入独立的 `.sandbox.env`，可从
`.sandbox.env.example` 复制：

```dotenv
MCP_CONTEXT7_TOKEN=replace-me
```

重建 sandbox 时显式传入该文件：

```bash
./scripts/up.sh alice --mcp-env-file .sandbox.env
```

## 创建和绑定 Server

Header 模板只能引用 `MCP_*` 变量，因此不会读取 Bridge token 或模型供应商 token。GET 响应
也只会返回模板，不会返回展开后的值。

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

也可以绑定已存在的 Session。`PUT` 是完整替换，空数组表示解绑全部：

```bash
curl -sS -X PUT "${BRIDGE_URL}/v1/sessions/${SESSION_ID}/mcp-servers" \
  -H "$AUTH" -H 'Content-Type: application/json' \
  -d "{\"server_ids\":[\"${MCP_SERVER_ID}\"]}" | jq
```

## 更新行为和限制

修改绑定时 Pi 正在生成会返回 `409 session_streaming`；否则新配置在下一条 Prompt 前通过重启
idle Pi 生效。更新 Server 定义不会中断运行中的 Pi，所有已绑定 Session 会在下一条非流式
Prompt 前使用新快照。

若缺少 Header 模板引用的环境变量，Prompt 返回 `422 mcp_environment_missing`。正在被绑定的
Server 不能删除，需先解绑。

默认 MCP URL 必须为 HTTPS。仅在可信内网开发服务确实使用 HTTP 时，才在 `.sandbox.env`
设置 `MCP_ALLOW_INSECURE_HTTP=1` 后重建 sandbox。模型供应商域名不应配置给 Pi：模型请求只会
经 Docker 私网中的 LiteLLM 转发。

API 鉴权变量的准备方法见 [HTTP API](api.md)，域名放行方式见
[网络与安全](network-security.md)。

返回[文档索引](README.md)。
