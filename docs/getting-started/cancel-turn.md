# 取消 Agent Turn

取消接口是显式动作：

```bash
curl -fsS -X POST \
  "$MANAGER_URL/v1/instances/$SUBJECT_REF/sessions/$SESSION_ID/turns/$TURN_ID:cancel" \
  -H "$AUTH" | jq
```

取消请求会由 Manager 转发给 Bridge 和 Pi。调用方仍应继续读取事件并查询 Turn，直到它进入终态，
因为取消与模型输出、工具调用完成之间可能存在短暂竞态。

取消不会删除 Session、对话历史或工作区文件。后续可以在同一 Session 中提交新的 Turn ID。
