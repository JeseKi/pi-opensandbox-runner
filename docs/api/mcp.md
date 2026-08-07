# MCP 管理 API

MCP Server 由 admin token 管理：

```http
GET    /admin/v1/mcp/servers
POST   /admin/v1/mcp/servers
PUT    /admin/v1/mcp/servers/{server_id}
DELETE /admin/v1/mcp/servers/{server_id}
```

```json
{
  "server_id": "context7",
  "label": "Context7",
  "transport": "streamable_http",
  "url": "https://mcp.context7.com/mcp",
  "auth": {"type":"bearer_token", "value":"secret"}
}
```

支持 `streamable_http` 和 `sse`；不支持 stdio、OAuth 或 SigV4。响应只返回
`credential_configured`，不会返回认证原值。

Policy 通过 `mcp_server_ids` 授权。修改 Server 后，使用它的 ready Instance 会在下一条 Prompt
前重启 Pi 以加载变更。
