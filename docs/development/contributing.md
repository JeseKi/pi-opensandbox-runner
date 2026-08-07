# 扩展与贡献指南

修改代码或文档时遵守以下原则：

- Manager API 是稳定边界，变更 schema 时同步 OpenAPI、测试和文档。
- 不把 Bridge 内部 ID、URL 或 credential 暴露到 consumer API。
- 新的生命周期动作使用持久化 Operation，并定义清晰的幂等语义。
- 新配置项包含默认值、环境变量、示例和部署文档。
- 新文件能力明确说明路径边界、scope 和并发写入条件。
- 新 LiteLLM 能力说明对 virtual key、Policy 和 Agent 的影响。

提交前运行：

```bash
make check
make docs-build
```
