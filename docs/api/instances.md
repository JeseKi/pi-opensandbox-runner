# Runner Instance API

```http
GET  /v1/instances
PUT  /v1/instances/{subject_ref}
GET  /v1/instances/{subject_ref}
POST /v1/instances/{subject_ref}:reconcile
POST /v1/instances/{subject_ref}:stop
POST /v1/instances/{subject_ref}:destroy
```

确保 Instance：

```bash
curl -fsS -X PUT "$MANAGER_URL/v1/instances/user-1" \
  -H "$AUTH" -H 'Content-Type: application/json' \
  -d '{"policy_slug":"consumer-default"}' | jq
```

ensure、reconcile、stop 和 destroy 返回 `202 AcceptedOperation`。destroy 还要求：

```http
X-Confirm-Destroy: user-1
```

只有 `ready` Instance 可以创建 Session 或访问数据面。`subject_ref` 只在当前 consumer 内唯一。
