# Egress Policy

每个 Runner Policy 定义额外的出站域名白名单。Manager 创建 Sandbox 时生成：

```json
{
  "defaultAction": "deny",
  "egress": [
    {"action":"allow", "target":"litellm"},
    {"action":"allow", "target":"opensandbox"},
    {"action":"allow", "target":"pypi.org"}
  ]
}
```

`litellm` 和 `opensandbox` 自动放行；其他域名来自 `egress_domains`。允许域名意味着 Sandbox 内
代码可以向该域发送请求，因此应遵循最小权限，避免宽泛通配符。

DNS+nft egress sidecar 禁用 IPv6，并以默认拒绝模式执行。域名 allowlist 不能替代应用层鉴权，
也不能防止已允许站点上的恶意内容。
