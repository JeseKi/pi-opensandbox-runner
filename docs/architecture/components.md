# 组件与职责

| 组件 | 主要职责 | 不负责 |
| --- | --- | --- |
| 上层 consumer | 用户鉴权、产品 Session/Turn、队列、业务审计 | Sandbox 和模型供应商凭据 |
| Runner Manager | Policy、Instance、Operation、Session/Turn 映射、凭据编排 | 最终用户体系和产品 UI |
| Pi Bridge | Pi RPC、对话历史、事件、文件、命令和终端代理 | 跨 Sandbox 调度和 consumer 鉴权 |
| OpenSandbox | Sandbox、endpoint、持久卷、execd 和网络接入 | 产品 Session/Turn |
| Pi | Agent 推理循环、工具调用和上下文 | Sandbox 生命周期 |
| LiteLLM | 模型路由、virtual key、预算、限流和 MCP Gateway | Runner 生命周期 |

组件间不共享 master credential。consumer 只持有 Manager service token；Sandbox 只得到属于该
Instance 的 LiteLLM virtual key。
