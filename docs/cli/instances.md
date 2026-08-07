# Runner Instance 命令

## 查看状态

```bash
uv run pi-runner-manager-cli --token "$MANAGER_TOKEN" status user-1
```

## 校正当前 Instance

```bash
uv run pi-runner-manager-cli --token "$MANAGER_TOKEN" reconcile user-1
```

## 停止 Instance

```bash
uv run pi-runner-manager-cli --token "$MANAGER_TOKEN" stop user-1
```

## 销毁 Instance

```bash
uv run pi-runner-manager-cli --token "$MANAGER_TOKEN" destroy user-1
```

后三个动作只提交异步 Operation，不等待终态。CLI 当前没有 ensure 子命令；创建或重新启用
Instance 请调用 `PUT /v1/instances/{subject_ref}`。

`subject_ref` 会直接进入 URL path，建议只使用 ASCII 字母、数字、`.`、`_`、`~`、`-`。
