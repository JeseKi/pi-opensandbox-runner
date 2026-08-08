# 常见问题

## 为什么 ensure 返回 202 后还不能创建 Session？

Instance 创建是异步 Operation。等待 Operation `succeeded` 且 Instance `ready`。

## 为什么 Manager ready，但 Agent 无法调用模型？

Manager ready 不实时检查 LiteLLM、provider 或 virtual key。检查 Turn、LiteLLM 日志与 Policy。

## stop 会删除工作区吗？

不会。stop/destroy 当前都保留命名卷。

## `cwd` 是否限制 Agent 只能访问一个目录？

不是。它只是初始工作目录。

## 为什么文档站和 API Reference 使用两个地址？

`/mkdocs/docs` 是面向使用者的叙述式文档，`/v1/docs` 是由 OpenAPI 生成的接口 schema 与调试页。

## 修改 catalog 后为什么已有 Instance 没变化？

catalog 发布新 revision；已有 Instance 在下一次 ensure 时切换。reconcile 只校正当前 revision。
