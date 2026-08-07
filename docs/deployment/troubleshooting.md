# 部署故障排查

## Manager 未 ready

```bash
docker compose ps
docker compose logs --tail=200 manager
```

重点检查 catalog 是否有效、Manager DB 是否可写、加密 key 是否为有效 Fernet key。

## Instance provision 失败

```bash
uv run pi-runner-manager-cli --token "$MANAGER_TOKEN" status "$SUBJECT_REF"
docker compose logs --tail=200 manager opensandbox litellm
```

根据 `problem.component`、`problem.code`、`problem.retryable` 和 Operation `phase` 定位。常见阶段包括
`issuing_model_key`、`creating_sandbox`、`waiting_sandbox`、`checking_bridge` 和 `applying_catalog`。

## 文档站返回 404

确认 Manager 镜像通过 `Dockerfile.manager` 构建，或本地先执行：

```bash
make docs-build
```

并确认 `RUNNER_MANAGER_DOCS_SITE_DIR` 指向生成的 `site` 目录。Manager 在启动时挂载该目录；
构建后需要重启本地 Manager 进程。
