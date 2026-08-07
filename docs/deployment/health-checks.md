# 健康检查

| 地址 | 含义 |
| --- | --- |
| `GET /healthz` | Manager HTTP 进程存活 |
| `GET /readyz` | Manager Operation worker 已运行 |
| Bridge `GET /healthz` | Bridge 进程存活 |
| Bridge `GET /readyz` | Bridge Session 组件已初始化 |

```bash
curl -fsS __MANAGER_ORIGIN__/healthz
curl -fsS __MANAGER_ORIGIN__/readyz
docker compose ps
```

Manager `/readyz` 不对 OpenSandbox、LiteLLM 和模型供应商执行实时探测。依赖故障通常体现在
provision/recovery Operation 的 `problem` 中。
