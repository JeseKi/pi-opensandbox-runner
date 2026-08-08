# 启动 Pi OpenSandbox Runner

Manager 可以作为本地 Python 进程直接运行；不要求 Manager 自己运行在 Docker 中。它需要能够访问
OpenSandbox 和 LiteLLM，这两个依赖可以是本机进程、容器或远程服务。

## 准备配置

```bash
make init-config
```

该命令会创建 `.litellm.env`、`.manager.env` 和 OpenSandbox 本地配置；自动生成并写入全部本地
secret，且不会打印它们。已有配置不会被覆盖。

然后只需编辑 `.litellm.env`，填写至少一个模型供应商的 API key，例如 `DEEPSEEK_API_KEY`。
`LITELLM_MASTER_KEY`、OpenSandbox API key、Manager 加密 key 和 service/admin token 已由初始化命令
正确配置，无需手动复制或生成。

## 直接启动 Manager（本地 CLI）

先构建文档静态文件：

```bash
make docs-build
```

如果 OpenSandbox 和 LiteLLM 在本机默认端口运行，直接启动：

```bash
make manager-run
```

该命令会读取 `.manager.env`，并使用以下默认依赖地址：

```text
OpenSandbox: http://127.0.0.1:8080
LiteLLM:     http://127.0.0.1:4000
Manager:     http://127.0.0.1:8090
```

依赖在其他地址时，在启动前覆盖即可：

```bash
export OPENSANDBOX_BASE_URL=http://opensandbox.internal:8080
export LITELLM_BASE_URL=http://litellm.internal:4000
make manager-run
```

Manager 在前台运行，按 `Ctrl-C` 停止。启动后检查：

```bash
curl -fsS http://127.0.0.1:8090/readyz
```

## 使用 Docker Compose 启动完整本地环境

```bash
docker build -t pi-opensandbox-runner:local .
docker build -f Dockerfile.egress -t pi-runner-egress:local .
docker compose up -d --build
```

## 验证

```bash
docker compose ps
curl -fsS __MANAGER_ORIGIN__/healthz
curl -fsS __MANAGER_ORIGIN__/readyz
```

成功后可访问：

- 使用文档：`__MANAGER_ORIGIN__/mkdocs/docs/`
- API Reference：`__MANAGER_ORIGIN__/v1/docs`

下一步：[创建 Runner Instance](create-instance.md)。
