# 创建 Runner Instance

Runner Instance 对应一个 consumer 下的稳定主体，通常是用户或租户。确保 Instance 是异步且
幂等的操作。

```bash
response="$({
  curl -fsS -X PUT \
    "$MANAGER_URL/v1/instances/$SUBJECT_REF" \
    -H "$AUTH" \
    -H 'Content-Type: application/json' \
    -d '{"policy_slug":"consumer-default"}'
})"
printf '%s\n' "$response" | jq
export OPERATION_ID="$(printf '%s' "$response" | jq -r '.operation.id')"
```

接口返回 `202 Accepted`，不表示 Sandbox 已经可用。轮询 Operation：

```bash
while true; do
  operation="$(curl -fsS \
    "$MANAGER_URL/v1/operations/$OPERATION_ID" \
    -H "$AUTH")"
  printf '%s\n' "$operation" | jq '{status, phase, problem}'
  status="$(printf '%s' "$operation" | jq -r '.status')"
  case "$status" in
    succeeded) break ;;
    failed) exit 1 ;;
  esac
  sleep 1
done
```

只有 Instance `state` 为 `ready` 时才能创建 Session。

```bash
curl -fsS "$MANAGER_URL/v1/instances/$SUBJECT_REF" -H "$AUTH" | jq
```

下一步：[创建 Session](create-session.md)。
