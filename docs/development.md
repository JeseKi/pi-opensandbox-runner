# 本地开发与集成

## 开发环境

安装依赖并运行静态检查与测试：

```bash
uv sync
uv run ruff check .
uv run mypy src
uv run pytest
```

直接运行 Bridge：

```bash
export PI_DEFAULT_MODEL=coding-default
uv run pi-opensandbox-runner
```

## 面向 agent-runner 的集成

后续接入 `agent-runner` 项目时，推荐把本项目视为每用户 sandbox 的数据面：

- agent-runner 保存 OpenSandbox sandbox ID、Bridge URL 与 Bridge proxy token；
- 用户操作映射到本项目的 Session、Prompt、events 和 entries API；
- SSE `seq` 作为断线续传位置；
- OpenSandbox Server 仍是内部控制面，不把其 API key 暴露给最终用户。

当前 Bearer token 是容器级 proxy token：持有者可管理该容器中的全部 Pi Session。若未来一个
容器承载多个不互信用户，应在 agent-runner 网关层做用户与 sandbox 的绑定，或改为每 Session
授权。

具体请求和游标语义见 [HTTP API](api.md)，部署边界见
[网络与安全](network-security.md)。

返回[文档索引](README.md)。
