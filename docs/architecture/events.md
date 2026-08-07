# 消息与事件流转

用户输入经过 Manager 和 Bridge 到达 Pi；模型输出及工具生命周期事件反向进入 Bridge journal，
再由 consumer 分页读取。

| 信息 | 来源 | 经过 | 最终持有者 |
| --- | --- | --- | --- |
| Turn input | consumer | Manager、Bridge | Pi Session history |
| 模型请求 | Pi | LiteLLM | 模型供应商 |
| Agent 事件 | Pi | Bridge、Manager | consumer |
| 文件修改 | Pi/consumer | Execd/Bridge | Workspace volume |
| Turn 状态 | Bridge/Manager | Manager DB | consumer |

事件 `seq` 在 Session 事件流中用于排序和去重。cursor 是读取位置，不应被解释为数据库 ID。
consumer 应保存业务所需的最终消息和审计记录，不能把 Bridge 的有限事件分段当作永久消息库。
