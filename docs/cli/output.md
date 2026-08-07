# CLI 输出与退出状态

成功响应以格式化 JSON 输出到标准输出。Manager HTTP 错误的 Problem JSON 输出到标准错误。

| 退出状态 | 含义 |
| --- | --- |
| `0` | 查询成功或异步 Operation 已被接受 |
| `1` | Manager HTTP 错误、网络错误或响应解析失败 |
| `2` | CLI 参数错误 |

`reconcile`、`stop`、`destroy` 返回 `0` 仅表示 Manager 接受操作。使用 `status` 或 Operation API
确认最终结果。

不要把 token 写入 shell history。可在交互式 shell 中读取：

```bash
read -rsp 'Manager service token: ' MANAGER_TOKEN
printf '\n'
export MANAGER_TOKEN
```
