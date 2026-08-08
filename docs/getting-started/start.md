# 启动 Pi OpenSandbox Runner

## 准备配置

```bash
make init-config
```

该命令会创建 `.litellm.env`、`.manager.env` 和 OpenSandbox 本地配置；自动生成并写入全部本地
secret，且不会打印它们。已有配置不会被覆盖。

然后只需编辑 `.litellm.env`，填写至少一个模型供应商的 API key，例如 `DEEPSEEK_API_KEY`。
`LITELLM_MASTER_KEY`、OpenSandbox API key、Manager 加密 key 和 service/admin token 已由初始化命令
正确配置，无需手动复制或生成。

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
