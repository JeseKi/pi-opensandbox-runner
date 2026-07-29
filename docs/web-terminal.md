# WebTerminal API

Runner Manager 提供 Instance 级交互式 PTY API，但不提供产品 UI。外部系统可以使用
xterm.js 或其他终端组件实现 WebTerminal。

Terminal 会启动一个独立 Bash，与 Pi RPC 进程共享同一个 OpenSandbox、文件系统和网络。
它不是“附着到 Agent stdin”，Agent 的输入、取消和事件仍使用 Turn API。Terminal 的 cwd
也不是权限边界：shell 可以访问整个 sandbox，并可能与正在运行的 Agent 并发修改 workspace。

## 数据链路

```mermaid
flowchart LR
    ui[Browser / xterm.js] -->|WSS + one-time ticket| manager[Manager]
    manager -->|Bridge Bearer| os[OpenSandbox server proxy]
    os --> bridge[Pi Bridge]
    bridge -->|WebSocket| execd[Execd PTY]
    execd --> shell[Bash PTY]
```

浏览器只持有 60 秒有效且只能使用一次的连接 ticket。Manager service token、Bridge token、
OpenSandbox API key 和 Execd 地址都不会进入浏览器。

## 配置

生产环境必须设置：

```dotenv
RUNNER_MANAGER_TERMINAL_PUBLIC_WS_URL=wss://runner.example.com/v1/terminal-connections
RUNNER_MANAGER_TERMINAL_ALLOWED_ORIGINS=https://app.example.com
```

Origin 使用逗号分隔的精确值，不支持通配符。生产必须使用 `wss://`。默认限制为每个 Instance
4 个 Terminal、ticket 60 秒、断线保留 15 分钟、Terminal 绝对生命周期 8 小时。

反向代理只需向浏览器公开 WebSocket 路径，Manager 其他 API 继续留在内部网络：

```nginx
location = /v1/terminal-connections {
    proxy_pass http://manager:8090;
    proxy_http_version 1.1;
    proxy_set_header Upgrade $http_upgrade;
    proxy_set_header Connection "upgrade";
    proxy_read_timeout 9h;
}
```

TLS、最终用户登录和 subject_ref 授权仍由外部业务系统负责。

## 创建与连接

外部后端先用 service token 创建 Terminal。可选 `session_id` 只用于选择初始 cwd：

```bash
MANAGER_URL=http://127.0.0.1:8090
AUTH='Authorization: Bearer <manager-service-token>'

TERMINAL="$(
  curl -sS -X POST \
    "$MANAGER_URL/v1/instances/user-1/terminals" \
    -H "$AUTH" -H 'Content-Type: application/json' \
    -d '{"session_id":"session-1"}'
)"
TERMINAL_ID="$(jq -r .id <<<"$TERMINAL")"
```

同一外部 Session 的 Terminal 可以用 Manager 的数据库级筛选分页查询：

```bash
curl -sS \
  "$MANAGER_URL/v1/instances/user-1/terminals?session_id=session-1&state=created&limit=100" \
  -H "$AUTH" | jq
```

`session_id` 不存在时返回 `404 session_not_found`；`state` 对 Terminal 状态做精确筛选，
并与 `session_id` 按 AND 组合。使用 `next_cursor` 翻页时必须保持所有筛选条件不变。

完成最终用户授权后，后端为浏览器页面的精确 Origin 签发 ticket：

```bash
curl -sS -X POST \
  "$MANAGER_URL/v1/instances/user-1/terminals/$TERMINAL_ID/tickets" \
  -H "$AUTH" -H 'Content-Type: application/json' \
  -d '{"origin":"https://app.example.com"}' | jq
```

响应中的 `websocket_url` 和 `subprotocols` 应原样交给已经授权的浏览器：

```js
const socket = new WebSocket(ticket.websocket_url, ticket.subprotocols);
socket.binaryType = "arraybuffer";
```

不要让浏览器调用 ticket REST API，也不要把 Manager service token 放入前端代码、
Local Storage 或 URL。

## WebSocket 协议

协议与 OpenSandbox Execd PTY 保持一致：

| 方向 | 帧 | 含义 |
| --- | --- | --- |
| Browser → Manager | binary `0x00 + bytes` | stdin |
| Manager → Browser | binary `0x01 + bytes` | stdout |
| Manager → Browser | binary `0x03 + uint64be offset + bytes` | replay |
| Browser → Manager | `{"type":"resize","cols":120,"rows":40}` | 调整 PTY |
| Browser → Manager | `{"type":"signal","signal":"SIGINT"}` | 发送信号 |
| 双向 | JSON | connected、ping/pong、error、exit |

单帧最大 64 KiB。前端应累计 stdout 字节数作为 replay cursor。断线后获取新 ticket，并连接
`websocket_url?since=<last-offset>`；需要从另一个页面接管相同 shell 时追加
`takeover=1`。被接管的旧连接会收到 close code `4001`。

结束使用时应主动删除：

```bash
curl -sS -X DELETE \
  "$MANAGER_URL/v1/instances/user-1/terminals/$TERMINAL_ID" \
  -H "$AUTH"
```

## Demo

本地 `.manager.env` 允许 `http://127.0.0.1:8000,http://localhost:8000` 后：

```bash
python -m http.server 8000 -d docs
```

打开 `http://127.0.0.1:8000/examples/web-terminal.html`，用上述 REST 请求生成新 ticket，
把完整 ticket JSON 粘贴到页面并连接。Demo 不接收 service token。

Demo 为便于直接运行使用固定版本和 SRI 的 CDN 资源。生产 WebTerminal 应自行打包或托管
xterm.js，使用严格 CSP，不应加载广告、动态脚本或其他不可信第三方内容。
