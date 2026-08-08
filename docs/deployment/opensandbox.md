# OpenSandbox 配置

`make init-config` 会生成 `.runtime/opensandbox.toml` 和对应 API key，并写入 `.manager.env`：

```bash
make init-config
```

生成配置的关键约束：

- Docker runtime 与 `opensandbox/execd:v1.0.21`
- Sandbox 加入 `pi-runner-internal` 网络
- Sandbox 端口不发布到宿主机，由 OpenSandbox proxy 访问 Bridge
- 禁止 host path storage，使用命名持久卷
- 删除高风险 Linux capabilities，并启用 `no_new_privileges`
- egress 使用 `dns+nft` 模式且默认拒绝
- OpenSandbox 控制状态使用独立 SQLite volume

`.manager.env` 中的 `OPENSANDBOX_API_KEY` 必须与 `.runtime/server.json` 的
`server_api_key` 相同。
