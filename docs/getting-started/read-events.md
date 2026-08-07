# 读取 Agent 事件

Manager 通过游标分页接口提供 Session 事件。调用方应保存 `next_cursor` 并持续轮询。

```bash
cursor=0
while true; do
  page="$(curl -fsS \
    "$MANAGER_URL/v1/instances/$SUBJECT_REF/sessions/$SESSION_ID/events?cursor=$cursor&limit=100" \
    -H "$AUTH")"
  printf '%s\n' "$page" | jq '.items[]'
  cursor="$(printf '%s' "$page" | jq -r '.next_cursor')"

  turn_status="$(curl -fsS \
    "$MANAGER_URL/v1/instances/$SUBJECT_REF/sessions/$SESSION_ID/turns/$TURN_ID" \
    -H "$AUTH" | jq -r '.status')"
  case "$turn_status" in
    succeeded|failed|cancelled) break ;;
  esac
  sleep 1
done
```

事件包含协议版本、单调递增的 `seq`、Session ID、Turn ID、类型和数据。consumer 应按 `seq`
去重，并使用 `turn_id` 将事件归属到产品 Turn；不要依赖 Bridge 内部 command ID。

事件保留量有限，不能代替 consumer 自己的业务审计和消息存储。
