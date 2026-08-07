# Web Terminal API

## 创建 Terminal

```http
POST /v1/instances/{subject_ref}/terminals
```

请求可带 `session_id` 决定初始 cwd；省略时使用 `/root/workspace`。

## 签发浏览器 ticket

```http
POST /v1/instances/{subject_ref}/terminals/{terminal_id}/tickets
Content-Type: application/json

{"origin":"https://app.example.com"}
```

响应包含 `websocket_url`、两个 `subprotocols` 和过期时间。浏览器使用返回的 subprotocol 连接：

```javascript
const socket = new WebSocket(ticket.websocket_url, ticket.subprotocols)
```

WebSocket 地址为 `/v1/terminal-connections`。ticket 绑定精确 Origin、短期有效且只能消费一次。
Manager service token 永远不进入浏览器。

Terminal 可查询、分页列出和删除；状态包括 `created`、`connected`、`detached`、`exited`、
`closed`、`unavailable`。
