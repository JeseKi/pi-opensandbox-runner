# Runner Policy 配置

Policy 同时约束 Sandbox 资源、可用模型、LiteLLM 用量、MCP 权限和网络访问。

```toml
[[policies]]
slug = "consumer-default"
label = "Consumer Default"
model_slugs = ["coding-default"]
default_model_slug = "coding-default"
cpu = "2"
memory = "4Gi"
max_active_sessions = 4
max_budget = 5.0
budget_duration = "24h"
rpm_limit = 30
tpm_limit = 1000000
max_parallel_requests = 2
mcp_server_ids = []
egress_domains = ["github.com", "pypi.org"]
```

Policy 更新会发布新 revision。已有 Instance 在下一次 ensure 时应用新 revision 并重新
provision；`reconcile` 只校正当前 revision，不负责切换 Policy。

egress 项只接受域名或 `*.example.com` 通配形式，不接受 URL、端口、路径或裸 IP。
`litellm` 与 `opensandbox` 由 Manager 自动加入 allowlist。
