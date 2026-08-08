# 读取 Agent 事件

Agent 的文本、工具调用和结束信号都以事件形式提供。事件接口是轮询接口，不是 SSE；调用方应保存
`next_cursor`，下次从该位置继续读取，并按 `seq` 去重。

## 执行前：设置变量和起始游标

以下变量延续上一节。首次读取使用 `CURSOR=0`：

```bash
export MANAGER_URL=__MANAGER_ORIGIN__
export MANAGER_TOKEN='<.manager.env 中的 RUNNER_MANAGER_BOOTSTRAP_SERVICE_TOKEN>'
export SUBJECT_REF='user-1'
export SESSION_ID='session-1'
export TURN_ID='turn-1'
export AUTH="Authorization: Bearer $MANAGER_TOKEN"
export CURSOR=0
```

`CURSOR` 不是 Turn ID。它只是事件日志的位置：首次从 `0` 开始，之后只使用服务返回的
`next_cursor`，不要自行递增或解析它。

## 1. 读取一页事件

```bash
page="$(curl -fsS \
  "$MANAGER_URL/v1/instances/$SUBJECT_REF/sessions/$SESSION_ID/events?cursor=$CURSOR&limit=100" \
  -H "$AUTH")"
printf '%s\n' "$page" | jq
```

响应示例：

```json
{
  "items": [
    {
      "protocol_version": 1,
      "seq": 1,
      "session_id": "session-1",
      "turn_id": "turn-1",
      "occurred_at": "2026-08-08T06:50:02Z",
      "type": "agent_end",
      "data": {},
      "source": "pi",
      "raw": {"seq": 1, "source": "pi", "event": {"type": "agent_end"}}
    }
  ],
  "next_cursor": "1"
}
```

- `items`：本批新增事件；可能为空，空数组不表示 Turn 必然结束。
- `seq`：单调递增事件序号，用于去重。
- `turn_id`：将事件关联回业务侧的 Turn。
- `type` 与 `data`：事件类型和载荷。应对未知类型保持兼容，选择透传或忽略，不能使消费循环中断。
- `next_cursor`：下一次请求必须原样带回的游标。

## 2. 保存下一游标

从刚才的响应取出下一游标：

```bash
export CURSOR="$(printf '%s' "$page" | jq -r '.next_cursor')"
printf '%s\n' "$CURSOR"
```

示例输出：

```text
1
```

## 3. 持续读取直到 Turn 结束（可选）

生产调用方应将 cursor 和已处理的 `seq` 持久化到自己的数据库。最小轮询示例如下：

```bash
while true; do
  page="$(curl -fsS \
    "$MANAGER_URL/v1/instances/$SUBJECT_REF/sessions/$SESSION_ID/events?cursor=$CURSOR&limit=100" \
    -H "$AUTH")"
  printf '%s\n' "$page" | jq '.items[]'
  CURSOR="$(printf '%s' "$page" | jq -r '.next_cursor')"

  turn="$(curl -fsS \
    "$MANAGER_URL/v1/instances/$SUBJECT_REF/sessions/$SESSION_ID/turns/$TURN_ID" \
    -H "$AUTH")"
  status="$(printf '%s' "$turn" | jq -r '.status')"
  case "$status" in
    succeeded|failed|cancelled)
      printf '%s\n' "$turn" | jq '{id, status, problem}'
      break
      ;;
  esac
  sleep 1
done
```

循环结束时，`succeeded` 表示 Agent 已完成；`failed` 或 `cancelled` 时读取响应中的 `problem` 处理。
事件保留量有限，不能替代调用方自己的业务审计和消息存储。
