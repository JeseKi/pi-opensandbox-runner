# Bridge 配置项

Bridge 配置由 Manager 在创建 Sandbox 时注入。业务 consumer 不应直接设置这些值。

| 环境变量 | 默认值 | 说明 |
| --- | --- | --- |
| `BRIDGE_STATE_ROOT` | `/root/.pi/bridge` | Bridge SQLite 与事件目录 |
| `PI_CODING_AGENT_SESSION_DIR` | `/root/.pi/agent/sessions` | Pi 对话历史目录 |
| `PI_WORKSPACE_ROOT` | `/root/workspace` | 工作区根目录 |
| `PI_EXECUTABLE` | `pi` | Pi 可执行文件 |
| `OPENSANDBOX_EXECD_URL` | `http://127.0.0.1:44772` | Execd 地址 |
| `PI_DEFAULT_MODEL` | `coding-default` | 默认 model slug |
| `PI_MODEL_CATALOG_PATH` | `/root/.pi/bridge/models.json` | 下发给 Pi 的模型目录 |
| `LITELLM_API_BASE` | `http://litellm:4000/v1` | 模型 API 地址 |
| `LITELLM_VIRTUAL_KEY` | 空 | 当前 Instance 的 LiteLLM key |
| `PI_MAX_ACTIVE_SESSIONS` | `4` | 最大活动 Session 数 |
| `PI_IDLE_TIMEOUT_SECONDS` | `300` | 空闲 Pi 进程回收时间 |
| `PI_RPC_TIMEOUT_SECONDS` | `30` | Pi RPC 调用超时 |
| `PI_STOP_GRACE_SECONDS` | `10` | 停止 Pi 的宽限时间 |
| `PI_LITELLM_MCP_ENABLED` | false | 是否加载 LiteLLM MCP Gateway |
| `BRIDGE_TRUSTED_PROXY_HOST` | `opensandbox` | 唯一受信代理主机名 |
| `BRIDGE_TRUSTED_PROXY_CACHE_SECONDS` | `30` | 代理地址 DNS 缓存时间 |

事件 journal 当前使用 8 MiB × 8 个分段的固定默认值。
