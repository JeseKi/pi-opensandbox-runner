# Manager CLI

`pi-runner-manager-cli` 是 Runner Manager 的轻量运维客户端，用于查看已发布的 Model 和
Policy，以及检查或变更某个 Runner Instance 的生命周期。它直接调用 Manager API，不应
用于访问 sandbox 内的 Bridge。

## 调用方式

在项目目录中使用 `uv run`：

```bash
uv run pi-runner-manager-cli --help
```

安装项目后也可以直接调用：

```bash
pi-runner-manager-cli --help
```

全局参数必须写在子命令之前：

```text
pi-runner-manager-cli [--base-url URL] --token TOKEN COMMAND
```

| 参数 | 必填 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `--base-url` | 否 | `http://127.0.0.1:8090` | Runner Manager 地址 |
| `--token` | 是 | 无 | 绑定 consumer 的 Manager service token |

CLI 不会自动读取 `.manager.env`。本地交互式运维时，可以隐藏输入并把 service token 放入
只对当前 shell 有效的环境变量：

```bash
read -rsp 'Manager service token: ' MANAGER_SERVICE_TOKEN
printf '\n'
export MANAGER_SERVICE_TOKEN
uv run pi-runner-manager-cli \
  --token "$MANAGER_SERVICE_TOKEN" \
  models
```

不要把 token 写入 shell 历史、脚本源码或日志。`RUNNER_MANAGER_BOOTSTRAP_ADMIN_TOKEN`
创建的 admin token 没有绑定 consumer，不能用于这些 CLI 命令。

## Catalog 命令

### `models`

列出当前已发布的 Model：

```bash
uv run pi-runner-manager-cli \
  --token "$MANAGER_SERVICE_TOKEN" \
  models
```

输出包括 Model slug、显示名称、Provider Model、API 类型、上下文窗口、最大输出 token 和
reasoning 能力。该命令只读取 Manager catalog，不验证对应 LiteLLM 上游此刻是否可用。

### `policies`

列出每个 Policy 最新的已发布 revision：

```bash
uv run pi-runner-manager-cli \
  --token "$MANAGER_SERVICE_TOKEN" \
  policies
```

输出包括 Policy slug、revision、可用 Model、默认 Model 和发布状态。

## Instance 命令

`subject_ref` 是上层 consumer 为最终用户或租户分配的稳定标识，例如 `user-1`。同一个
service token 只能访问其所属 consumer 的 Instance。当前 CLI 没有对路径参数执行 URL
编码，因此只应使用由 ASCII 字母、数字、`.`、`_`、`~`、`-` 组成的单个路径段；不要包含
`/`、`?`、`#`、空格或非 ASCII 字符。

### `status SUBJECT_REF`

读取 Instance 当前状态：

```bash
uv run pi-runner-manager-cli \
  --token "$MANAGER_SERVICE_TOKEN" \
  status user-1
```

重点字段：

- `state`：Instance 的生命周期状态，例如 `provisioning`、`ready`、`stopped` 或 `failed`。
- `phase`：当前或最近一次操作的细分阶段。
- `problem`：失败详情；排障时关注 `code`、`detail` 和 `retryable`。
- `policy_slug`、`policy_revision`：当前应用的 Policy。

### `reconcile SUBJECT_REF`

提交 provision Operation，重新校正 LiteLLM key、sandbox、Bridge readiness 和 Model
catalog：

```bash
uv run pi-runner-manager-cli \
  --token "$MANAGER_SERVICE_TOKEN" \
  reconcile user-1
```

`reconcile` 会复用当前实例凭据和匹配的 sandbox，不会主动轮换凭据。新 Operation 的
`status` 通常是 `pending`，`phase` 通常是 `queued`；这只表示 Operation 已持久化入队，
不表示校正已经完成。可继续运行 `status` 观察 Instance 的 `state`、`phase` 和 `problem`。

不要使用 `reconcile` 恢复 `stopped` 或 `destroyed` Instance：这两个操作完成后，Manager
已经清空实例凭据，当前 API 仍可能接受 reconcile 请求，但后台 provision 最终会以
`instance_credentials_missing` 失败。恢复这类 Instance 必须由上层 consumer 重新执行
ensure；当前 CLI 没有 ensure 子命令。

### `stop SUBJECT_REF`

提交 stop Operation：

```bash
uv run pi-runner-manager-cli \
  --token "$MANAGER_SERVICE_TOKEN" \
  stop user-1
```

完成后 Manager 会删除当前 sandbox、吊销 LiteLLM virtual key，并把 Instance 标记为
`stopped`。Pi 历史和 workspace 命名卷仍然保留，后续再次 ensure Instance 时可以恢复。

### `destroy SUBJECT_REF`

提交 destroy Operation：

```bash
uv run pi-runner-manager-cli \
  --token "$MANAGER_SERVICE_TOKEN" \
  destroy user-1
```

CLI 会自动发送值等于 `subject_ref` 的 `X-Confirm-Destroy` 请求头。完成后 Manager 删除当前
sandbox、吊销 LiteLLM virtual key，并把 Instance 标记为 `destroyed`。

> 当前实现仍保留 Pi 历史和 workspace 命名卷，也允许上层再次 ensure 同一
> `subject_ref`。永久删除命名卷需要执行独立的受控回收流程。不要按模糊前缀批量删除卷。

`destroy` 与 `stop` 当前的主要差别是最终 Instance 状态和操作意图；两者都异步执行，也都
不会由 CLI 等待到终态。

## 输出与退出状态

成功响应以格式化 JSON 输出到标准输出。Manager 返回的 HTTP 错误以 Problem JSON 输出到
标准错误。

| 退出状态 | 含义 |
| --- | --- |
| `0` | Manager 接受请求或成功返回查询结果 |
| `1` | Manager 返回 HTTP 错误，或客户端发生未处理的网络/响应解析异常 |
| `2` | CLI 参数错误 |

对 `reconcile`、`stop` 和 `destroy` 而言，退出状态 `0` 只表示异步 Operation 已成功提交。
HTTP 错误会以 Problem JSON 输出到标准错误；网络、DNS、TLS 或响应解析异常目前可能同时输出
Python traceback。

## 常见错误

| HTTP 状态 | 常见原因 | 处理方式 |
| --- | --- | --- |
| `401` | token 缺失、无效或已停用 | 检查 `--token`，确认没有误用其他环境的 token |
| `403` | 使用 admin token，或 service token 缺少所需 scope | 改用绑定正确 consumer 的 service token |
| `404` | 当前 consumer 下不存在该 `subject_ref` | 检查 subject 拼写和 token 所属 consumer |
| `422` | `subject_ref` 长度等参数不符合 API 约束 | 检查命令参数；CLI 会自动填写销毁确认头 |

Manager API 的完整请求、Operation 查询和错误结构见 [Manager API](manager-api.md)，部署、
备份和资源回收流程见 [运行与维护](operations.md)。

返回[文档索引](README.md)。
