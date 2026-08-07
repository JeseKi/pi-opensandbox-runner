# 项目结构

```text
src/pi_opensandbox_manager/   Manager 控制面
src/pi_opensandbox_runner/    Sandbox 内 Bridge 数据面
extensions/pi-runner-mcp/     Pi 到 LiteLLM MCP 的扩展
config/runner-catalog.toml    Model 与 Policy catalog
litellm/config.yaml           LiteLLM provider 路由
scripts/                      本地编排、entrypoint 和网络 patch
tests/                        单元与集成测试
docs/                         MkDocs 文档源
mkdocs.yml                    文档站导航和主题配置
compose.yaml                  基础部署
```

Manager route 按领域拆分在 `app/`，长任务位于 `service/long_tasks.py`；Bridge 的 Session supervisor、
event journal 和 Execd client 位于各自模块。
