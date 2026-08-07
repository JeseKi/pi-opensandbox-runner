# API 鉴权

```http
Authorization: Bearer <token>
```

## Service token

绑定一个 consumer，用于 catalog、Instance、Session、Turn、文件、命令和 Terminal API。资源查询
自动限制在 token 所属 consumer。

## Admin token

不绑定 consumer，用于 `/admin/v1/mcp/servers` 等管理操作。admin token 不能替代 service token
访问 consumer 资源。

不同能力还受 scope 约束，例如 `sessions:read`、`sessions:write`、`workspace:read`、
`workspace:write`、`filesystem:access` 和 `terminals:access`。

不要把任何 Manager token 发送到浏览器。Web Terminal 使用短期一次性 ticket。
