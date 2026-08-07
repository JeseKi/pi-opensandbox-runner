# 文件系统安全

Runner 提供三种不同风险级别的文件访问：

| 接口 | 边界 | 适用方 |
| --- | --- | --- |
| `workspace-files` | 强制限定 `/root/workspace`，拒绝 symlink 逃逸 | 产品文件页 |
| Session `workspace` | 相对 Session cwd，前缀不是强安全边界 | 可信内部功能 |
| `filesystem` | 任意绝对容器路径 | 已完成授权的高可信后端 |

文本更新最大 1 MiB、要求 UTF-8 和 `If-Match`。上传新文件要求 `If-None-Match: *`。ETag 用于防止
并发覆盖，但不是访问控制凭据。

Agent 本身运行在 Sandbox 内，可以访问其容器和挂载卷；`cwd` 只决定初始目录，不构成权限边界。
