# 运行测试

```bash
make lint
make test
make docs-build
make check
```

- Ruff 检查格式和常见错误。
- mypy 以 strict 模式检查 `src`。
- pytest 运行 Manager、Bridge、catalog、journal、MCP 和 egress 测试。
- MkDocs 使用 `--strict`，未解析链接和配置 warning 会导致失败。

提交前至少运行 `make check` 和 `make docs-build`。涉及 Dockerfile、Compose、Pi 或 OpenSandbox
patch 时还应构建镜像并执行相应端到端链路。
