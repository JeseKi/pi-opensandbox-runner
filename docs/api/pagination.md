# API 分页与游标

Instance、Session 和 Terminal 列表使用不透明 cursor：

```json
{
  "items": [],
  "next_cursor": null,
  "has_more": false
}
```

存在下一页时，把 `next_cursor` 原样传给下一次请求，并保持 `q`、`state`、`policy_slug`、
`model_slug`、`session_id` 等筛选条件不变。不要解析、拼接或长期缓存 cursor。

事件接口的 cursor 不同：它从 `0` 开始，响应的 `next_cursor` 是字符串形式的事件读取位置。
即使没有新事件，也应使用响应值继续读取。
