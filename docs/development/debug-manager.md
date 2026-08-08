# 调试 Manager

本地启动入口是：

```bash
make manager-run
```

它会加载 `.manager.env`，并默认连接本机的 OpenSandbox（`127.0.0.1:8080`）和 LiteLLM
（`127.0.0.1:4000`）。实际执行的 CLI 是 `uv run pi-runner-manager`；需要完全自行管理环境变量时，
也可以直接运行该命令。

本地启动前配置 Manager 所需环境变量，并确保 catalog 路径和数据库目录有效。Manager 默认监听
`0.0.0.0:8090`。

常用入口：

- `/v1/docs`：请求 schema 和手动调用
- `/v1/openapi.json`：生成的协议定义
- `/readyz`：worker 状态
- Instance/Operation API：provision phase 和 problem

若希望本地进程同时提供文档，先执行 `make docs-build`，确保当前工作目录存在 `site/`，再启动
Manager。
