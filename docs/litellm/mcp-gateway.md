# LiteLLM MCP Gateway

Manager 通过 LiteLLM Admin API 注册远程 MCP Server。Pi 只连接统一入口：

```text
http://litellm:4000/mcp/
```

并使用当前 Instance 的 virtual key。Policy 的 `mcp_server_ids` 被写入 key 的 MCP object
permission，因此未授权 Server 即使已经注册，也不能被该 Instance 调用。

支持：

- Streamable HTTP
- SSE
- 静态 Bearer、API key、Basic 等认证值
- 可选 allowed tools

当前不支持 stdio、OAuth 和 AWS SigV4。MCP credential 由 Manager 转交 LiteLLM，Bridge 不保存
Server 或认证值。
