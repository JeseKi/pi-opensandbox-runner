# Turn API

```http
PUT  /v1/instances/{subject_ref}/sessions/{session_id}/turns/{turn_id}
GET  /v1/instances/{subject_ref}/sessions/{session_id}/turns/{turn_id}
POST /v1/instances/{subject_ref}/sessions/{session_id}/turns/{turn_id}:cancel
```

提交体：

```json
{"input":"检查工作区并修复测试"}
```

PUT 返回 `202`。Turn 状态包括 `queued`、`running`、`succeeded`、`cancelled` 和 `failed`。
同一 Session 最多一个活动 Turn。

调用方应先把任务和输入持久化到自己的数据库，再调用 Manager。请求超时后使用相同 Turn ID 和
相同 input 重试。
