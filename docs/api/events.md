# Event API

```http
GET /v1/instances/{subject_ref}/sessions/{session_id}/events?cursor=0&limit=100
```

响应：

```json
{
  "items": [{
    "protocol_version": 1,
    "seq": 1,
    "session_id": "session-1",
    "turn_id": "turn-1",
    "occurred_at": "2026-01-01T00:00:00Z",
    "type": "agent_end",
    "data": {}
  }],
  "next_cursor": "1"
}
```

首次使用 `cursor=0`；之后原样传回 `next_cursor`。当前接口是轮询，不是 SSE。未知事件类型应
忽略或透传，不能中断消费循环。`limit` 范围为 1–1000。
