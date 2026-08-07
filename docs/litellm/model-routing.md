# Model Catalog 与 LiteLLM 路由

模型存在两个需要保持一致的配置面：

| 配置 | 作用 |
| --- | --- |
| `config/runner-catalog.toml` | Manager/Policy 元数据以及 Pi 可见能力 |
| `litellm/config.yaml` | LiteLLM 实际 provider 路由和 credential 引用 |

`runner-catalog.toml` 的 `models[].slug` 必须对应 `litellm/config.yaml` 的 `model_name`。

```yaml
model_list:
  - model_name: coding-default
    litellm_params:
      model: deepseek/deepseek-v4-flash
      api_key: os.environ/DEEPSEEK_API_KEY
```

Manager 下发的 `context_window`、`max_tokens`、`reasoning` 和 `input` 是声明值，不会自动探测
provider。新增或变更模型时必须同时验证两侧配置。
