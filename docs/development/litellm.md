# 集成 LiteLLM

本地 LiteLLM 配置位于 `litellm/config.yaml`，credential 位于 `.litellm.env`。新增模型时：

1. 添加或更新 LiteLLM `model_list` route。
2. 在 `.litellm.env` 配置 provider credential。
3. 使用同一个 model alias 更新 `runner-catalog.toml`。
4. 将 model slug 加入目标 Policy。
5. 验证 Manager catalog、Instance provision 和真实 Agent Turn。

MCP 端到端测试可启用 Compose 的 `e2e` profile，其中包含只供测试使用的 Bearer MCP Server。
