# Session API

```http
GET    /v1/instances/{subject_ref}/sessions
PUT    /v1/instances/{subject_ref}/sessions/{session_id}
GET    /v1/instances/{subject_ref}/sessions/{session_id}
DELETE /v1/instances/{subject_ref}/sessions/{session_id}
```

```json
{
  "title": "修复项目测试",
  "model_slug": "coding-default",
  "cwd": "/root/workspace/projects/example"
}
```

Session ID 由 consumer 生成，在 Instance 内唯一。列表读取 Manager 快照，单 Session GET 会向
Bridge 刷新运行状态。删除 Session 会删除其运行映射，但不会自动删除整个 Workspace volume。

`legacy_bridge_session_id` 和 `legacy_cwd` 仅用于旧数据迁移，新接入方必须省略。
