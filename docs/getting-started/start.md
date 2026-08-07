# 启动 Pi OpenSandbox Runner

## 准备环境文件

```bash
cp .litellm.env.example .litellm.env
cp .manager.env.example .manager.env
chmod 600 .litellm.env .manager.env
```

填写 `.litellm.env` 中的 `LITELLM_MASTER_KEY` 和至少一个模型供应商密钥。填写
`.manager.env` 中的 OpenSandbox API key、同一个 LiteLLM master key、Manager 凭据加密 key，
以及两个不同的 service/admin bootstrap token。

```bash
bash -c 'source scripts/lib.sh; ensure_server_config'
jq -r .server_api_key .runtime/server.json
uv run python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())'
openssl rand -hex 32
```

## 构建并启动

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
- Swagger：`__MANAGER_ORIGIN__/v1/docs`

下一步：[创建 Runner Instance](create-instance.md)。
