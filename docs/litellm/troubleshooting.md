# LiteLLM 故障传播与排查

## 检查服务

```bash
docker compose ps litellm litellm-db
docker compose logs --tail=200 litellm litellm-db
```

开发覆盖启用后可检查：

```bash
curl -fsS http://127.0.0.1:4000/health/liveliness
```

## 常见故障

| 现象 | 检查项 |
| --- | --- |
| provision 在 key 阶段失败 | master key、LiteLLM DB、Policy key 参数 |
| model not found | catalog slug 与 LiteLLM `model_name` |
| provider 401 | `.litellm.env` credential 与 provider route |
| 429 | virtual key budget、RPM、TPM、并发 |
| MCP Server 不可见 | Policy `mcp_server_ids`、virtual key permission、Pi reload |
| Agent 流中断 | LiteLLM/Provider 日志、超时和网络 egress |

LiteLLM 故障不会让 Manager `/readyz` 失败；通常通过 Operation problem 或具体 Turn/Agent 事件暴露。
