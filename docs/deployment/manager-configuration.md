# Manager 配置项

| 环境变量 | 默认值 | 说明 |
| --- | --- | --- |
| `RUNNER_MANAGER_DATABASE_URL` | `sqlite:///./data/runner-manager.db` | Manager SQLAlchemy 数据库 URL |
| `OPENSANDBOX_BASE_URL` | `http://opensandbox:8080` | OpenSandbox Server 地址 |
| `OPENSANDBOX_API_KEY` | 空 | OpenSandbox 管理 API key；部署必填 |
| `LITELLM_BASE_URL` | `http://litellm:4000` | LiteLLM 管理地址 |
| `LITELLM_MASTER_KEY` | 空 | LiteLLM 管理 key；部署必填 |
| `RUNNER_MANAGER_CREDENTIAL_ENCRYPTION_KEY` | 空 | 加密数据库内 Bridge/virtual key 的 Fernet key |
| `PI_RUNNER_IMAGE` | `pi-opensandbox-runner:local` | Sandbox 使用的 Runner 镜像 |
| `RUNNER_MANAGER_BOOTSTRAP_CONSUMER` | `agent-runner` | 启动时创建的 consumer slug |
| `RUNNER_MANAGER_BOOTSTRAP_SERVICE_TOKEN` | 空 | consumer service token |
| `RUNNER_MANAGER_BOOTSTRAP_ADMIN_TOKEN` | 空 | Manager admin token |
| `RUNNER_MANAGER_CATALOG_PATH` | `config/runner-catalog.toml` | Model/Policy catalog 路径 |
| `RUNNER_MANAGER_CATALOG_RELOAD_SECONDS` | `5` | catalog 重载周期 |
| `RUNNER_MANAGER_OPERATION_POLL_SECONDS` | `0.5` | Operation worker 空闲轮询周期 |
| `RUNNER_MANAGER_HTTP_TIMEOUT_SECONDS` | `30` | Manager 调用依赖的 HTTP 超时 |
| `RUNNER_MANAGER_PROVISION_TIMEOUT_SECONDS` | `180` | 等待 Sandbox ready 的上限 |
| `RUNNER_MANAGER_SANDBOX_HEALTHCHECK_SECONDS` | `60` | ready Instance 巡检周期 |
| `RUNNER_MANAGER_HOST` | `0.0.0.0` | Manager 监听地址 |
| `RUNNER_MANAGER_PORT` | `8090` | Manager 监听端口 |
| `RUNNER_MANAGER_DOCS_SITE_DIR` | `site` | 挂载到 `/mkdocs/docs` 的静态站点目录 |

## Web Terminal

| 环境变量 | 默认值 | 说明 |
| --- | --- | --- |
| `RUNNER_MANAGER_TERMINAL_PUBLIC_WS_URL` | `ws://127.0.0.1:8090/v1/terminal-connections` | 返回给浏览器的 WebSocket URL |
| `RUNNER_MANAGER_TERMINAL_ALLOWED_ORIGINS` | 空 | 精确 Origin 白名单，逗号分隔 |
| `RUNNER_MANAGER_TERMINAL_TICKET_TTL_SECONDS` | `60` | 一次性 ticket 有效期 |
| `RUNNER_MANAGER_TERMINAL_DETACHED_TTL_SECONDS` | `900` | 断开终端保留时间 |
| `RUNNER_MANAGER_TERMINAL_MAX_LIFETIME_SECONDS` | `28800` | 终端最大生命周期 |
| `RUNNER_MANAGER_MAX_TERMINALS_PER_INSTANCE` | `4` | 每个 Instance 最大终端数 |
| `RUNNER_MANAGER_TERMINAL_CLEANUP_SECONDS` | `60` | 终端清理周期 |

修改密钥或监听配置后需重启 Manager。catalog 文件由后台周期重载，无需重启。
