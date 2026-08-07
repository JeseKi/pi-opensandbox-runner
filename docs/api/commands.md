# Command API

```http
POST   /v1/instances/{subject_ref}/sessions/{session_id}/commands
GET    /v1/instances/{subject_ref}/sessions/{session_id}/commands/{command_id}
DELETE /v1/instances/{subject_ref}/sessions/{session_id}/commands/{command_id}
```

```json
{
  "command": "pytest -q",
  "timeout": 600000,
  "envs": {"CI": "1"}
}
```

Manager 将命令强制为后台执行，并使用 Session cwd。POST 返回 command ID；GET 查询状态和输出；
DELETE 尝试终止。命令能力可以执行任意 Sandbox shell，不应直接暴露给不可信客户端。
