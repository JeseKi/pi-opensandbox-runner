# 外部 MCP

MCP Server 由 Runner Manager 管理，并由 LiteLLM MCP Gateway 转发。Bridge 不再保存 Server、认证值或
Session 绑定；每个 Pi 进程只连接 `http://litellm:4000/mcp/`，并携带该 Runner Instance 的 LiteLLM
virtual key。

当前支持远程 Streamable HTTP 和 SSE Server，以及静态认证值；不支持 stdio、OAuth 或 AWS SigV4。

## 管理 Server

仅 Manager admin token 可以管理 Server。认证值仅转发给 LiteLLM，列表与响应只返回
`credential_configured`，不会返回原始值。

```bash
curl -sS -X POST "${MANAGER_URL}/admin/v1/mcp/servers" \
  -H "Authorization: Bearer ${MANAGER_ADMIN_TOKEN}" \
  -H 'Content-Type: application/json' \
  -d '{
    "server_id":"context7",
    "label":"Context7",
    "transport":"streamable_http",
    "url":"https://mcp.context7.com/mcp",
    "auth":{"type":"bearer_token","value":"replace-me"}
  }' | jq
```

`PUT /admin/v1/mcp/servers/{server_id}` 修改定义，`DELETE` 删除。更新或删除后，Manager 会让使用该
Server 的 ready Instance 在下一条 Prompt 前滚动重启 Pi；正在生成的请求不会被中断。

## Policy 授权

在 `config/runner-catalog.json` 的 Policy 中配置 `mcp_server_ids`。Manager 创建 LiteLLM virtual key
时会把这些 Server ID 写入 `object_permission.mcp_servers`；未列出的 Server 即使存在于 Gateway 中也
不可被该 Instance 调用。

```json
{
  "slug": "consumer-default",
  "mcp_server_ids": ["context7"]
}
```

删除一个仍被活动 Instance 所用的 Server 会返回 `409 mcp_server_in_use`。若 Policy 引用了不存在的
Server，Instance provisioning 会失败并返回 `mcp_policy_invalid`。

## 本地认证链路验证

仓库提供了仅用于端到端验证的 Bearer-token MCP Server，定义在 `compose.dev.yaml`。执行
`docker compose -f compose.yaml -f compose.dev.yaml --profile e2e up -d` 后，它位于
`http://mcp-auth-test:8766/mcp`，认证值为 `test-mcp-secret`，并提供
`authenticated_echo` 工具。该服务不对宿主机发布端口，也不应用于生产。

返回[文档索引](README.md)。
