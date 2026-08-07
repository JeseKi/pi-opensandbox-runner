# Runner 日常运维

## 日常检查

```bash
docker compose ps
curl -fsS __MANAGER_ORIGIN__/readyz
uv run pi-runner-manager-cli --token "$MANAGER_TOKEN" models
uv run pi-runner-manager-cli --token "$MANAGER_TOKEN" policies
```

## Instance 检查

```bash
uv run pi-runner-manager-cli --token "$MANAGER_TOKEN" status user-1
uv run pi-runner-manager-cli --token "$MANAGER_TOKEN" reconcile user-1
```

重点监控 Operation failed 数量、失败 phase、磁盘/volume、Manager DB、LiteLLM DB、Sandbox 数量和
模型供应商错误。`reconcile` 用于校正当前 Instance，不是无条件重建或凭据轮换命令。
