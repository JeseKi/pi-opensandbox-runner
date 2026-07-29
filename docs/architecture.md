# 架构设计

本文描述 Pi OpenSandbox Runner 的逻辑实体、持久化数据，以及一次提示词从调用方到模型再返回
事件流的过程。

## 实体关系

下图同时描述 SQLite 中的逻辑实体和与其一一对应或按名称关联的持久化文件。其中只有
`SESSION_MCP_SERVER` 的两条关系是数据库外键；其余关系由 Bridge 的运行时协议或命名卷维护。

```mermaid
erDiagram
    SANDBOX ||--|| BRIDGE_STATE : "挂载 /root/.pi"
    BRIDGE_STATE ||--o{ SESSION : "保存元数据"
    BRIDGE_STATE ||--o{ MCP_SERVER : "保存定义"
    SESSION ||--o{ SESSION_MCP_SERVER : "绑定"
    MCP_SERVER ||--o{ SESSION_MCP_SERVER : "被绑定"
    SESSION ||--o{ EVENT_SEGMENT : "产生事件"
    SESSION ||--o| PI_SESSION_JSONL : "生成历史"
    SANDBOX ||--|| MODEL_CATALOG : "持久化 models.json"
    SANDBOX ||--|| WORKSPACE_VOLUME : "挂载工作目录"
    LITELLM_PROXY ||--o{ VIRTUAL_KEY : "签发"
    VIRTUAL_KEY ||--|| SANDBOX : "限定模型访问范围"

    SESSION {
        string id "主键"
        string name "名称"
        string cwd "初始工作目录"
        string model "LiteLLM 模型别名"
        string thinking_level "思考等级"
        string session_file "Pi 历史文件"
    }
    MCP_SERVER {
        string id "主键"
        string name "唯一名称"
        string transport "传输方式"
        string url "服务地址"
    }
    SESSION_MCP_SERVER {
        string session_id "Session 外键"
        string server_id "MCP Server 外键"
    }
    EVENT_SEGMENT {
        string session_id "所属 Session"
        integer sequence "事件序号"
        string ndjson_path "NDJSON 路径"
    }
    PI_SESSION_JSONL {
        string session_id "所属 Session"
        string path "文件路径"
    }
    MODEL_CATALOG {
        string fingerprint "配置指纹"
        string path "文件路径"
    }
    VIRTUAL_KEY {
        string models "可用模型"
        integer tpm_limit "每分钟令牌上限"
        number max_budget "总预算"
        string expiry "过期时间"
    }
```

## 一次提示词的流转

`provider` 在 Bridge API 中固定为 LiteLLM，不再是调用方参数；调用方只选择已授权的模型
别名。模型供应商密钥始终停留在 LiteLLM 容器，sandbox 只持有受限的 virtual key。

```mermaid
flowchart LR
    subgraph caller[调用方]
        request[POST /v1/sessions/:id/prompts\nBearer bridge proxy token + model]
        sse[读取 SSE / entries]
    end

    subgraph sandbox[OpenSandbox 私网沙箱]
        subgraph bridge[FastAPI Bridge]
            auth[来源校验与模型白名单校验]
            session[读取/更新 Session 元数据]
            restart{模型目录或运行配置\n是否已变更?}
            supervisor[会话监督器\n启动或复用 Pi RPC]
            journal[写入 Event Journal]
        end
        subgraph persistence[持久卷 /root/.pi]
            sqlite[(SQLite：会话与 MCP 绑定)]
            catalog[models.json\n原子替换]
            history[(Pi session JSONL)]
            events[(分段 NDJSON)]
        end
        pi[Pi RPC 子进程\n固定使用 LiteLLM]
    end

    subgraph gateway[LiteLLM 私网网关]
        key[验证沙箱虚拟密钥\n模型、预算、RPM/TPM]
        route[按模型别名路由]
    end

    subgraph upstream[模型供应商]
        model[DeepSeek / OpenAI / Anthropic]
    end

    request --> auth
    auth -->|无效令牌 / 未授权模型| reject[401 / 422]
    auth --> session
    session <--> sqlite
    session --> restart
    catalog -.模型目录指纹.-> restart
    restart -->|是，且未生成| supervisor
    restart -->|正在生成| pending[409：模型目录更新待处理]
    restart -->|否| supervisor
    supervisor --> pi
    pi -->|OpenAI 兼容请求\n虚拟密钥| key
    key -->|限流 / 预算 / 模型限制| gateway_error[LiteLLM 4xx 错误]
    key --> route
    route -->|供应商密钥仅在此处使用| model
    model --> route --> pi
    pi --> history
    pi --> journal --> events
    journal --> sse
```

有关容器、鉴权与网络隔离边界，参见[网络与安全](network-security.md)。有关数据卷生命周期和
模型目录更新，参见[运行与维护](operations.md)。

返回[文档索引](README.md)。
