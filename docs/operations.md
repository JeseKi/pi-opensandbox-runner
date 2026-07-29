# 运行与维护

本文介绍 sandbox 生命周期、LiteLLM virtual key、模型目录、构建镜像源和数据恢复。

## 启动与生命周期

启动一个名为 `alice` 的完整实例：

```bash
./scripts/up.sh alice \
  --egress-profile github \
  --model coding-default
```

新 sandbox 必须显式选择至少一个公网 egress profile，或提供自定义域名清单；LiteLLM 私网
地址会自动加入，不能移除。可用的内置 profile 为 `github`、`npm-global` 与 `npm-cn`。多个
profile 可以同时传入，例如：

```bash
./scripts/up.sh alice \
  --egress-profile github \
  --egress-profile npm-global \
  --model coding-default
```

脚本会：

1. 启动本地 OpenSandbox Server；
2. 在本地 Pi Bridge 镜像不存在时构建镜像；
3. 创建两个命名卷；
4. 通过 OpenSandbox API 创建 sandbox；
5. 输出 Bridge URL，并将独立的外部代理 Bearer token 写入权限为 `0600` 的状态文件。

`alice` 是逻辑名称，不强依赖 Docker 自动生成的容器名。运行信息和 token 保存在
`.runtime/alice.json`，权限为 `0600`。默认不会把 token 输出到终端，避免泄漏到 shell
历史、终端采集或 CI 日志。确实需要显示时，显式传入 `--show-token`：

```bash
./scripts/up.sh alice \
  --show-token \
  --egress-profile github \
  --model coding-default
```

常用生命周期命令：

```bash
./scripts/status.sh alice
./scripts/down.sh alice
./scripts/up.sh alice --model coding-default
```

`down.sh` 删除 sandbox，但保留 Pi 历史和 workspace 命名卷。再次 `up.sh` 会恢复它们。
永久删除数据需要显式确认：

```bash
./scripts/destroy.sh alice --yes
```

升级到外部 proxy token 模型前创建的 sandbox 仍可暂时使用旧 Bridge 鉴权。要移除其容器内的
旧 Bridge token，重新创建 sandbox：

```bash
./scripts/down.sh alice
./scripts/up.sh alice --egress-profile github --model coding-default
```

这会签发新 token 和 LiteLLM virtual key，但不会删除两个持久卷。

## 构建镜像源

构建时默认使用 `MIRROR_MODE=auto`：若可访问 Google 则使用官方 Debian、PyPI 和 npm 源，
否则切换到清华 Debian/PyPI 镜像及 npmmirror。也可以显式指定，避免自动探测带来的不确定性：

```bash
./scripts/up.sh alice \
  --mirror-mode cn \
  --egress-profile npm-cn \
  --model coding-default
```

可选值为 `auto`、`cn`、`global`，也可通过宿主机 `MIRROR_MODE` 环境变量设置默认值。选中的
npm、pip、uv 源会写入最终镜像，因此容器内的 Agent 后续运行这些安装器时仍会使用相同镜像
配置。

## 管理 virtual key 预算

LiteLLM 管理 UI 位于 `http://127.0.0.1:4000/ui`，只发布到宿主机回环地址。使用
`.litellm.env` 中的 `LITELLM_MASTER_KEY` 登录；该 key 只应由受信任的宿主机管理员使用，绝不
注入 sandbox 或写入自动化日志。

每个 sandbox 的 virtual key 初始为 `$5` 预算、`24h` 预算周期，并在 `Asia/Shanghai` 时区的
每日零点自动重置；key 本身不按时间过期。打开 UI 的 Keys 页面后，按
`pi-runner-<sandbox 名称>-...` alias 或 `sandbox_name` metadata 定位对应 key，即可编辑
`Max Budget`、`Budget Duration` 和其他 LiteLLM 限制。这些改动会立即作用于运行中的
sandbox，无需重启 Pi。

需要提前恢复额度时，在对应 key 的详情页使用 **Reset Spend**，它会把当前周期 spend 置为
`$0`，但不会更换 key 字符串。不要使用 **Regenerate Key** 或 **Auto-Rotation**：它们会生成
新的 key，而运行中的 sandbox 不会自动取得新 secret。

UI 对单个 key 的改动不会跨 `down.sh` / `up.sh` 保留：旧 key 会被吊销，新 key 会重新使用
`litellm/config.yaml` 中的默认值。要修改未来 sandbox 的默认预算或周期，更新该文件后重启
LiteLLM 容器。已经存在的旧 24 小时到期 key，可在 UI 中设为 Never Expire 并补上预算周期，
或执行 `down.sh` 后再 `up.sh` 以签发新 key。

## 热更新 Pi 可选模型

默认模型别名 `coding-default` 在 `litellm/config.yaml` 与 `config/pi-models.json` 中定义，
默认路由到 DeepSeek V4 Pro。`gpt-5.6-terra` 已在 LiteLLM 中配置；需要先在目标 sandbox 的
virtual key 中授权，再通过 Bridge 模型目录接口加入该 sandbox。

模型目录是每个 sandbox 独立、持久化的配置。先在 LiteLLM UI 给该 sandbox 的 virtual key
授权模型，再读取目录并原子提交更新；Bridge 会在下一次安全请求前重启对应 Pi RPC 子进程，
不会重建 sandbox 容器。

```bash
BRIDGE_URL="$(jq -r .bridge_url .runtime/alice.json)"
BRIDGE_PROXY_TOKEN="$(jq -r .bridge_proxy_token .runtime/alice.json)"

curl -sS "${BRIDGE_URL}/v1/models/config" \
  -H "Authorization: Bearer ${BRIDGE_PROXY_TOKEN}" > models-config.json

# 在 models-config.json 的 providers.litellm.models 中加入 gpt-5.6-terra 元数据。
curl -sS -X PUT "${BRIDGE_URL}/v1/models/config" \
  -H "Authorization: Bearer ${BRIDGE_PROXY_TOKEN}" \
  -H "Content-Type: application/json" \
  --data-binary @models-config.json | jq
```

上传会检查 Pi 文件结构、固定的 LiteLLM 私网配置，以及当前 virtual key 是否已获该模型授权。
上传期间正在生成的 Session 不会中断；它结束前的新请求会返回
`409 model_catalog_update_pending`。

返回[文档索引](README.md)。
