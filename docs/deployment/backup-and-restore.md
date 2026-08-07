# 数据备份与恢复

Manager SQLite 启用 WAL，在线备份应使用 SQLite backup API：

```bash
docker compose exec -T manager python - <<'PY'
import sqlite3
source = sqlite3.connect("/app/data/runner-manager.db")
target = sqlite3.connect("/app/data/runner-manager.backup.db")
source.backup(target)
target.close()
source.close()
PY
```

恢复时停止 Manager，替换数据库后使用原 `RUNNER_MANAGER_CREDENTIAL_ENCRYPTION_KEY` 启动。
若加密 key 不匹配，数据库内的 Bridge 和 LiteLLM 凭据无法解密。

完整恢复还需要协调：

1. Manager SQLite
2. LiteLLM PostgreSQL
3. OpenSandbox 控制状态
4. Pi 和 Workspace 命名卷

恢复后对 Instance 执行 reconcile，检查 stale Sandbox 和 virtual key。
