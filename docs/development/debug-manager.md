# 调试 Manager

```bash
uv run pi-runner-manager
```

本地启动前配置 Manager 所需环境变量，并确保 catalog 路径和数据库目录有效。Manager 默认监听
`0.0.0.0:8090`。

常用入口：

- `/v1/docs`：请求 schema 和手动调用
- `/v1/openapi.json`：生成的协议定义
- `/readyz`：worker 状态
- Instance/Operation API：provision phase 和 problem

若希望本地进程同时提供文档，先执行 `make docs-build`，确保当前工作目录存在 `site/`，再启动
Manager。
