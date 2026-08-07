# Model 与 Policy API

```http
GET /v1/catalog/models
GET /v1/catalog/policies
```

Session 使用 model `slug`，Instance ensure 使用 Policy `slug`。consumer 不应复制 Policy 的预算、
资源或网络字段作为自己的配置来源。

```bash
curl -fsS "$MANAGER_URL/v1/catalog/models" -H "$AUTH" | jq
curl -fsS "$MANAGER_URL/v1/catalog/policies" -H "$AUTH" | jq
```

旧的管理写入接口仍存在，但 catalog 文件管理模式下固定返回 `409 catalog_file_managed`：

```http
POST /admin/v1/models
POST /admin/v1/policies
```
