# 运行与维护

## 服务与数据

Compose 包含：

| 服务 | 默认地址 | 持久化 |
| --- | --- | --- |
| Runner Manager | `127.0.0.1:8090` | `manager-data` 中的 SQLite |
| OpenSandbox | `opensandbox:8080`，仅 Docker 网络 | `opensandbox-control` |
| LiteLLM | `litellm:4000`，仅 Docker 网络 | 独立 PostgreSQL |
| LiteLLM PostgreSQL | 仅 control network | `litellm-db` |

Manager 与这些依赖位于私有 control network，并通过独立的 Manager ingress network 向宿主机
回环地址发布 `8090`。Runner sandbox 位于内部 runner network，并由 OpenSandbox server
proxy 提供受 token 保护的 Bridge endpoint。

宿主机排障确实需要直连 OpenSandbox 或 LiteLLM 时，复制 `.env.dev.example` 为 `.env` 后
重建服务；开发覆盖只在 `127.0.0.1` 发布 `8080` 和 `4000`。生产部署不应加载该覆盖文件。

## 启动和健康检查

```bash
docker build -t pi-opensandbox-runner:local .
docker build -f Dockerfile.egress -t pi-runner-egress:local .
docker compose up -d --build
docker compose ps
curl -fsS http://127.0.0.1:8090/healthz
curl -fsS http://127.0.0.1:8090/readyz
```

`healthz` 只表示 HTTP 进程存活；`readyz` 还确认后台 Operation worker 正在运行。它不保证
OpenSandbox、LiteLLM 或模型供应商此刻可用，依赖故障会记录到 Operation problem。

## Instance 与 Operation

Instance 生命周期通过异步 Operation 驱动：

- `provision`：签发 LiteLLM key、恢复/创建 sandbox、等待 Bridge、下发 model catalog。
- `recovery`：由后台巡检触发，删除失效 sandbox 后以原有持久卷、凭据和 Policy 重建；最多尝试 3 次。
- `stop`：删除 sandbox、吊销 key，保留 Manager 记录和命名卷。
- `destroy`：当前同样删除 sandbox、吊销 key并标记 destroyed，但仍保留命名卷。

查看状态：

```bash
uv run pi-runner-manager-cli --token "$MANAGER_SERVICE_TOKEN" status user-1
uv run pi-runner-manager-cli --token "$MANAGER_SERVICE_TOKEN" reconcile user-1
uv run pi-runner-manager-cli --token "$MANAGER_SERVICE_TOKEN" stop user-1
```

各子命令的参数、输出、退出状态和常见错误见 [Manager CLI](cli.md)。

Operation 的 `phase` 比笼统的状态更适合排障，例如 `issuing_model_key`、
`creating_sandbox`、`waiting_sandbox`、`checking_bridge`、`applying_catalog`。失败时先查看
`problem.code`、`problem.detail` 和 `problem.retryable`。

Manager 在启动时立即、随后每 `RUNNER_MANAGER_SANDBOX_HEALTHCHECK_SECONDS`（默认 60）秒检查
`ready` Instance。只有 OpenSandbox 明确返回 404 或非 `running`/`ready` 状态才自动恢复；网络错误、
超时和 5xx 不会误触发重建。三次恢复之间分别等待 5 秒、15 秒，最后一次失败后状态为 `failed`。

## Model 与 Policy

`config/runner-catalog.json` 是 Manager 模型与 Policy 的唯一写来源。文件统一定义：

- 可用模型与默认模型；
- CPU、内存和最大活动 Session；
- LiteLLM 预算周期、RPM、TPM 和并发；
- sandbox egress domains。

Compose 将该文件以只读方式挂载给 Manager；部署时原子替换文件，Manager 每
`RUNNER_MANAGER_CATALOG_RELOAD_SECONDS`（默认 5）秒校验并同步一次。有效变更会发布新的
不可变 revision；已有 Instance 在下一次 ensure 时切换并重新 provision。无效变更不会影响
最后一次有效 catalog，首次启动遇到无效或缺失配置会失败。

默认 `consumer-default` 放行 Dockerfile 使用的 GitHub、apt、pip、npm 官方源和国内镜像源，
方便可信 sandbox 在运行时安装依赖。`litellm` 与 `opensandbox` 始终由 Manager 额外放行，不能写入
配置文件的 `egress_domains`。

配置结构为 `{ "version": 1, "models": [...], "policies": [...] }`。`models` 条目包含
`slug`、`label`、`provider_model`、`api`、`secret_ref`、`context_window`、`max_tokens`、
`reasoning` 和 `input`；`input` 默认 `["text"]`，若模型支持视觉则设为
`["text", "image"]`。该声明会原样下发给 Pi，不会自动探测 LiteLLM 上游能力。`policies` 条目
包含现有 Policy 的全部资源、限流和 egress 字段。slug、模型引用和域名都在加载时校验。删除条目会将其从 catalog 下架：不能创建新的 Instance/Session，但历史实例
和 sandbox recovery 继续使用数据库保存的版本快照。

列出已发布配置：

```bash
uv run pi-runner-manager-cli --token "$MANAGER_SERVICE_TOKEN" models
uv run pi-runner-manager-cli --token "$MANAGER_SERVICE_TOKEN" policies
```

CLI 只读取已发布的 Manager catalog，不验证 LiteLLM 上游的实时可用性。

修改 `runner-catalog.json` 不会创建 LiteLLM 上游路由。新增模型前，仍须先在
`litellm/config.yaml` 或 LiteLLM 管理面配置并验证同名 alias。`provider_model` 和 `secret_ref`
仅作为 Manager 控制面元数据保存。旧的 `/admin/v1/models` 和 `/admin/v1/policies` 写接口保留，
但固定返回 `409 catalog_file_managed`。

业务管理员只为用户选择 Policy slug，不直接编辑 LiteLLM key 或 OpenSandbox 参数。使用新
Policy 再次 ensure Instance 会轮换实例凭据并重新 provision。`reconcile` 不轮换凭据；它复用
当前凭据和匹配的 sandbox，重新校正 LiteLLM key、Bridge readiness 和 model catalog。

## SQLite 备份与恢复

容器模式数据库位于 `manager-data:/app/data/runner-manager.db`，启用了 WAL。在线备份应使用
SQLite backup API，而不是只复制主 `.db` 文件：

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

随后从 volume 中复制备份文件。恢复前停止 Manager，保留 OpenSandbox 和 LiteLLM，然后替换
数据库并重新启动。`RUNNER_MANAGER_CREDENTIAL_ENCRYPTION_KEY` 必须与备份时一致，否则数据库
中的 Bridge/virtual key 无法解密。

Manager 数据库、OpenSandbox 状态、LiteLLM PostgreSQL 和 Runner 命名卷属于一个恢复集合。
只恢复其中一个可能产生 stale sandbox 或 key；恢复后应对实例执行 reconcile。

## 密钥轮换

- **service/admin token**：当前没有受支持的 revoke/rotate 管理接口。bootstrap 只添加新 token
  hash，不会停用旧 token；在补齐正式管理能力前，不应通过手工 SQL 作为常规轮换流程。
- **credential encryption key**：当前没有在线 re-encrypt 工具，不能直接替换环境变量。需要先
  停止服务并迁移所有加密字段。
- **LiteLLM master key**：同时更新 `.litellm.env` 与 `.manager.env`，再重启相关服务。
- **OpenSandbox API key**：同时更新 OpenSandbox config 与 `.manager.env`。

## 持久卷回收

stop/destroy 默认保留 Pi 历史和 workspace，以便恢复和防止误删。当前 Manager 尚未调用独立
volume delete API。永久回收必须先确认 subject 已无合规保留要求，再通过受控运维流程删除对应
`pi-manager-<digest>-pi` 与 `pi-manager-<digest>-workspace` 卷。

不要按模糊前缀批量删除 Docker volume。

## 低层脚本

旧的 `scripts/up.sh` 系列保留用于 Bridge 和 OpenSandbox 开发。它们直接签发 key、创建 sandbox
并把凭据写入 `.runtime/<name>.json`，不受 Manager 数据库管理。生产和 `agent-runner` 联调
都应使用 Manager API。

返回[文档索引](README.md)。
