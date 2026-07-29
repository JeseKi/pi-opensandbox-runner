# 网络与安全

本文说明 Agent 的文件权限、Bridge 代理鉴权、模型凭据边界和 sandbox 出站策略。

## 文件权限边界

`cwd` 只是 Pi 的初始工作目录，不是权限边界。默认目录是
`/root/workspace/<session-id>`，也可以在创建 Session 时指定任意容器内绝对路径。Pi 以 root
运行，可以直接读写容器内其他目录。

这不会让 Pi 访问宿主机或其他用户的容器；真正的隔离边界是 Docker/OpenSandbox 容器。不要向
sandbox 挂载不希望 Agent 访问的宿主机路径。当前 OpenSandbox Docker 运行时还会丢弃高风险
capability 并启用 `no_new_privileges`。

只有 `/root/.pi` 和 `/root/workspace` 默认位于持久卷中。若 Session 使用其他目录，目录内容可在
当前容器内读写，但容器删除后不会自动持久化。

文件和命令 API 的路径同样不受 workspace 限制，详见 [HTTP API](api.md)。

## Bridge 代理鉴权

默认只通过 OpenSandbox server proxy 提供 Bridge：compose 将该 server 固定发布到
`127.0.0.1:8080`。项目内的 `Dockerfile.opensandbox` 还将 OpenSandbox Docker runtime
自动分配的 Execd/egress 端口强制绑定到 `127.0.0.1`，不会在 `0.0.0.0` 发布随机端口。

若要让其他内网机器访问，请在宿主机上单独配置有认证的反向代理、VPN 或 SSH tunnel；不要直接
改为 Docker 全接口监听。

派生的 OpenSandbox server proxy 会按 sandbox metadata 中保存的 SHA-256 摘要校验 Bridge
proxy token，然后在转发前剥离 `Authorization`。因此 token 不会出现在 Pi 容器环境变量、进程
环境或 Bridge 配置中。

Bridge 仅接受本容器回环请求与 `opensandbox` 代理容器的真实 TCP 来源；同一 Docker 私网中的
其他 sandbox 不能绕过 proxy 直连。OpenSandbox 的管理 API key 同样不会转发。

当前 Bearer token 是容器级 proxy token：持有者可管理该容器中的全部 Pi Session。若一个容器
承载多个不互信用户，应在上游网关层做用户与 sandbox 的绑定，或改为每 Session 授权。

## 模型凭据与预算

Sandbox 与 LiteLLM 处于同一个 Docker 私网，模型供应商 API 密钥不会进入 sandbox；Pi 只能
使用它自己的 LiteLLM virtual key。LiteLLM 管理/调试端口仅发布到宿主机
`127.0.0.1:4000`；sandbox 仍只通过私网访问 `litellm:4000`。

每个 sandbox 拿到的 virtual key 仅允许项目模型白名单；默认预算为每日 `$5`
（`Asia/Shanghai` 零点重置），停止或销毁时会立即吊销。具体预算操作见
[运行与维护](operations.md)。

## 出站网络策略

每个 sandbox 都通过 OpenSandbox egress sidecar 使用 `dns+nft` 策略：默认拒绝所有出站连接，
只有显式允许的 FQDN 才能解析并连接。sidecar 保留 `NET_ADMIN`，sandbox 主容器不保留该能力；
IPv6 也会禁用，避免绕过 IPv4 nftables 规则。

Pi runner 对固定的 OpenSandbox v0.2.2 Docker backend 应用了受版本约束的补丁，使 sidecar
加入同一私网、sandbox 共享其网络命名空间；构建时若上游源码结构变化，补丁会失败而不会静默
失效。

首次创建受控 sandbox 时，`up.sh` 会自动构建本地 `pi-runner-egress:local` 镜像；它只调整
Docker 内置 DNS 与 egress DNS 重定向的规则顺序，避免命名网络绕过 FQDN 策略。修改该
Dockerfile 或脚本后，先执行：

```bash
docker build -f Dockerfile.egress -t pi-runner-egress:local .
```

然后重建 sandbox。

## 自定义域名清单

自定义清单使用“一行一个域名或 `*.example.com`”的文本格式：

```text
# 允许受信任的 MCP 服务
mcp.example.com
*.trusted.example.net
```

创建时合并 profile 与清单：

```bash
./scripts/up.sh alice \
  --egress-profile github \
  --egress-allowlist .sandbox-egress.txt \
  --model coding-default
```

解析后的策略不含任何 secret，会保存在 `.runtime/alice.json`，因此 `down.sh` 后重新 `up.sh`
会自动恢复它。

运行中的策略只能由宿主机管理员更改：

```bash
scripts/egress-policy.sh alice show
scripts/egress-policy.sh alice apply \
  --egress-profile github \
  --egress-profile npm-global
```

`apply` 会完整替换 allowlist，而不是追加规则；它始终保留内部 `litellm` 与 `opensandbox`
（后者仅为 Bridge 代理回连）。旧 sandbox 没有 egress sidecar，必须先执行 `down.sh` 再带
profile 或清单重新创建，不能在运行中升级为受控网络。

返回[文档索引](README.md)。
