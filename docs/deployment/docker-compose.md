# Docker Compose 部署

基础 `compose.yaml` 启动 OpenSandbox、LiteLLM、LiteLLM PostgreSQL 和 Runner Manager。只将
Manager 的 `8090` 发布到宿主机回环地址。

```bash
make init-config

docker build -t pi-opensandbox-runner:local .
docker build -f Dockerfile.egress -t pi-runner-egress:local .
docker compose up -d --build
```

初始化完成后，编辑 `.litellm.env`，填写至少一个模型供应商的 API key。其余本地 secret 已自动生成，
不会输出到终端；再次执行初始化不会覆盖已有值。

`Dockerfile.manager` 在独立构建阶段执行严格的 MkDocs 构建，然后只把静态产物复制到 Manager
镜像。运行中的 Manager 不需要 MkDocs 依赖。

开发时若需要从宿主机直连 OpenSandbox 和 LiteLLM：

```bash
cp .env.dev.example .env
docker compose up -d --build
```

开发覆盖只在回环地址发布 `8080` 和 `4000`；生产环境不要加载它。
