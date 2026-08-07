# CLI 概览

`pi-runner-manager-cli` 是面向 Manager service token 的轻量运维客户端。它提供 catalog 查询和
单个 Instance 生命周期操作，不直接访问 Bridge。

```text
pi-runner-manager-cli [--base-url URL] --token TOKEN COMMAND
```

```bash
uv run pi-runner-manager-cli --help
uv run pi-runner-manager-cli \
  --base-url __MANAGER_ORIGIN__ \
  --token "$MANAGER_TOKEN" \
  models
```

全局参数必须写在子命令前。CLI 不自动读取 `.manager.env`，也不接受 admin token。

| 参数 | 默认值 | 说明 |
| --- | --- | --- |
| `--base-url` | `http://127.0.0.1:8090` | Manager 地址 |
| `--token` | 无 | 必填的 consumer service token |
