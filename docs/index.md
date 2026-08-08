# Pi OpenSandbox Runner

Pi OpenSandbox Runner 是运行 Pi Agent 的内部控制面和数据面组合。它为上层业务系统创建持久
Sandbox，管理 Session 与 Turn，并通过 LiteLLM 统一模型路由、预算、限流和 MCP 访问。

<div class="grid cards" markdown>

-   :material-rocket-launch: **第一次使用**

    ---

    从部署 Manager 到创建 Session，并发送第一条 Agent 请求。

    [开始使用](getting-started/index.md)

-   :material-graph-outline: **理解运行链路**

    ---

    查看请求、凭据、模型调用和事件如何在各组件间流转。

    [查看架构](architecture/index.md)

-   :material-api: **接入 Manager API**

    ---

    面向内部 consumer 的稳定 HTTP API、鉴权和错误约定。

    [查看 API](api/index.md)

-   :material-tune: **部署和配置**

    ---

    配置 OpenSandbox、Runner Policy、模型和生产运行环境。

    [查看部署文档](deployment/requirements.md)

</div>

## 文档入口

- [文档站](index.md)
- [Manager API Reference](/v1/docs)
- [Manager OpenAPI](/v1/openapi.json)
- Manager 健康检查：[存活状态](/healthz)和[就绪状态](/readyz)

!!! info "API 边界"

    业务系统应调用 Runner Manager。Bridge API 是 Manager 与 Sandbox 内部数据面的协议，
    不应作为产品接入边界。
