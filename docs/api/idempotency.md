# API 幂等性

## Instance ensure

`PUT /v1/instances/{subject_ref}` 以 consumer + subject 为身份。相同 Policy revision 的重复 ensure
不会无条件重建 Sandbox。

## Session ensure

Session 使用 consumer 生成的稳定 ID。超时后使用相同路径和相同业务参数重试。

## Turn

Turn ID 同时是 Manager 幂等键和 Bridge `Idempotency-Key`：

- 相同 ID、相同 input：返回已有 Turn。
- 相同 ID、不同 input：`409 idempotency_conflict`。

调用方必须先持久化 ID 和 input。网络超时不能通过生成新 Turn ID 解决，否则可能导致重复执行。

## 条件文件写入

更新/删除使用 `If-Match` 防止覆盖并发修改；创建使用 `If-None-Match: *` 防止覆盖已有文件。
