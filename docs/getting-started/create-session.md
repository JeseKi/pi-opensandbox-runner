# 创建 Session

Session 表示一段可持续的 Agent 对话。Session ID 由 consumer 提供，并在目标 Instance 内唯一。

```bash
export SESSION_ID=session-1

curl -fsS -X PUT \
  "$MANAGER_URL/v1/instances/$SUBJECT_REF/sessions/$SESSION_ID" \
  -H "$AUTH" \
  -H 'Content-Type: application/json' \
  -d '{
    "title": "第一次 Agent 会话",
    "model_slug": "coding-default"
  }' | jq
```

省略 `cwd` 时，Manager 使用 `/root/workspace/sessions/{session_id}`。也可以显式指定容器内
绝对路径：

```json
{
  "title": "修复项目测试",
  "model_slug": "coding-default",
  "cwd": "/root/workspace/projects/example"
}
```

`cwd` 是 Agent 的初始工作目录，不是文件访问权限边界。`model_slug` 必须由当前 Runner Policy
授权。

下一步：[与 Agent 交互](interact-with-agent.md)。
