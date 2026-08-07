# Instance 停止与销毁

```bash
uv run pi-runner-manager-cli --token "$MANAGER_TOKEN" stop user-1
uv run pi-runner-manager-cli --token "$MANAGER_TOKEN" destroy user-1
```

两者都会异步删除当前 Sandbox、吊销 LiteLLM virtual key，并保留 Manager 记录和命名卷。

- stop 表示暂时停止，最终状态是 `stopped`。
- destroy 表示销毁意图，最终状态是 `destroyed`，API 要求确认 header。

当前两种状态都允许后续通过 ensure 重新创建 Instance。若合规语义要求不可恢复删除，还必须执行
独立的持久卷回收和业务记录处理。
