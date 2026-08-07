# 持久卷回收

每个 Instance 有 Pi state 和 Workspace 两个命名卷。stop/destroy 不自动删除它们，以避免误删
Agent 历史和用户文件。

回收前确认：

1. Instance 已停止或销毁且不会恢复。
2. consumer 已完成最终用户授权和数据保留检查。
3. 所需内容已有可验证备份。
4. 精确解析该 Instance 的卷名称，不按模糊前缀批量选择。
5. 删除后记录 subject、操作者、时间和具体卷名。

当前 Manager 没有公开的 volume delete API。卷删除属于宿主机管理操作，执行后通常不可由
Manager 恢复。
