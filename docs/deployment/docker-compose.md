# Docker Compose 部署

基础 `compose.yaml` 启动 OpenSandbox、LiteLLM、LiteLLM PostgreSQL 和 Runner Manager。只将
Manager 的 `8090` 发布到宿主机回环地址。

```bash
bash -c 'source scripts/lib.sh; ensure_server_config'
cp .litellm.env.example .litellm.env
cp .manager.env.example .manager.env
chmod 600 .litellm.env .manager.env

docker build -t pi-opensandbox-runner:local .
docker build -f Dockerfile.egress -t pi-runner-egress:local .
docker compose up -d --build
```

`Dockerfile.manager` 在独立构建阶段执行严格的 MkDocs 构建，然后只把静态产物复制到 Manager
镜像。运行中的 Manager 不需要 MkDocs 依赖。

开发时若需要从宿主机直连 OpenSandbox 和 LiteLLM：

```bash
cp .env.dev.example .env
docker compose up -d --build
```

开发覆盖只在回环地址发布 `8080` 和 `4000`；生产环境不要加载它。
