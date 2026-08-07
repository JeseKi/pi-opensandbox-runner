# Model 与 Policy 命令

## `models`

```bash
uv run pi-runner-manager-cli --token "$MANAGER_TOKEN" models
```

列出已发布的 model slug、供应商元数据、上下文窗口、最大输出 token、reasoning 和输入模态。
该命令不实时探测 LiteLLM 上游。

## `policies`

```bash
uv run pi-runner-manager-cli --token "$MANAGER_TOKEN" policies
```

列出每个 Policy 最新的已发布 revision、允许模型和默认模型。catalog 的写入来源是
`config/runner-catalog.toml`，CLI 只读。
