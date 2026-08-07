# LiteLLM 对 Agent 的影响

LiteLLM 的配置会直接改变 Agent 行为：

- 路由决定 `model_slug` 最终调用哪个供应商模型。
- catalog 声明决定 Pi 的上下文窗口、最大输出、reasoning 和输入模态认知。
- virtual key 决定 Agent 可用模型及 MCP Server。
- 预算和限流可能中断生成或阻止新的模型请求。
- LiteLLM 的超时、重试和 provider 兼容行为会影响流式响应和工具循环。

因此排查 Agent “模型不可用”“输出被截断”“MCP 工具消失”时，不能只看 Bridge；应同时检查
catalog、Policy、virtual key 和 LiteLLM route。
