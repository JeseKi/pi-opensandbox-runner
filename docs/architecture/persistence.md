# 数据存储与持久化

| 数据 | 存储位置 | 生命周期 |
| --- | --- | --- |
| consumer、Policy、Instance、Operation、映射 | Manager SQLite | 控制面持久化 |
| provider、virtual key、MCP 配置 | LiteLLM PostgreSQL | 独立于 Manager DB |
| Bridge Session 索引 | Pi 命名卷中的 Bridge SQLite | 随 Instance 数据保留 |
| Pi 对话历史 | Pi 命名卷中的 JSONL | 跨 Sandbox 重建保留 |
| Agent 事件 | Pi 命名卷中的分段 NDJSON | 有容量上限 |
| 用户项目文件 | Workspace 命名卷 | 跨 Sandbox 重建保留 |

Manager SQLite、LiteLLM PostgreSQL、OpenSandbox 状态以及两个命名卷构成一个恢复集合。只恢复其中
一部分可能留下 stale Sandbox、失效 key 或不一致映射。
