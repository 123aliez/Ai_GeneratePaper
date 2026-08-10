# smolagents 双协议模型接入与推理深度控制方案

> OpenAI-compatible + Claude Native Messages API

> **最终技术决策**
>
> OpenAI 系列继续使用 OpenAI-compatible 接口与 smolagents；Claude 为保留完整能力，直接使用 Anthropic 原生 /v1/messages 协议。上层统一“模型路由”和“reasoning_level”，底层不强行统一 API 协议。LiteLLM 从核心调用链移除。

版本 v1.0 \| 2026-08-10

适用范围：smolagents Agent 系统、自建 Anthropic-compatible Claude 网关、OpenAI-compatible 模型服务

# 1. 最终推荐方案

系统采用“双协议原生接入”：OpenAI 路径保留 smolagents 的 OpenAIModel；Claude 路径使用 Anthropic Python SDK 直接调用自建 Anthropic-compatible 接口的 /v1/messages。应用层提供统一 Model Router，对模型选择、推理深度、超时、重试和流式输出做配置管理。

核心原则：统一的是业务配置和路由，不统一 OpenAI 与 Anthropic 的底层消息协议。这样既减少 LiteLLM 带来的额外超时链路，也避免 Claude 的 thinking、tool_use、prompt caching、document/citation、server tools 等能力在 OpenAI 兼容层中被压平。

| **路径** | **框架 / SDK**         | **底层协议**       | **定位**                                    |
|----------|------------------------|--------------------|---------------------------------------------|
| OpenAI   | smolagents OpenAIModel | OpenAI-compatible  | 通用 Agent、OpenAI/兼容模型                 |
| Claude   | Anthropic Python SDK   | POST /v1/messages  | Claude 完整能力优先                         |
| 统一层   | Model Router / 配置层  | 不直接生成模型请求 | 选择 Provider、映射 reasoning、控制运行参数 |

# 2. 已确认上下文与边界

| **项目项**  | **已确认事实**                     | **设计影响**                                   |
|-------------|------------------------------------|------------------------------------------------|
| Agent 框架  | 当前系统使用 smolagents            | 保留现有 OpenAI Agent 路径，减少重构范围       |
| OpenAI 接口 | 现有服务支持 OpenAI-compatible API | OpenAI 模型继续走 OpenAIModel                  |
| Claude 接口 | 现有自建接口支持 Anthropic 协议    | Claude 直接走 /v1/messages                     |
| Claude 目标 | 要求尽可能使用 Claude 完整能力     | 不把 Claude 强制转换成 /v1/chat/completions    |
| 现有问题    | LiteLLM 链路存在长响应/中断风险    | 核心路径移除 LiteLLM，采用流式和显式超时       |
| 推理深度    | OpenAI 与 Claude 都需要可配置      | 建立统一 reasoning_level，再做 Provider 级映射 |

> **“自建 Claude”的定义**
>
> 本文将“自建 Claude”理解为：你自建了 Anthropic-compatible 网关/代理，并由该服务转发到 Claude，而不是在本地部署 Claude 权重。Claude 专有能力能否实际可用，最终取决于该网关是否完整透传 Anthropic 字段和响应块。

# 3. 端到端架构

推荐调用链如下：

| **业务系统 / API / Frontend**                                    |                    |                            |
|------------------------------------------------------------------|--------------------|----------------------------|
| **Model Router（provider + model + reasoning_level + timeout）** |                    |                            |
| OpenAI Agent Engine                                              | 公共 Tool Registry | Claude Native Agent Engine |
| smolagents OpenAIModel                                           | 工具定义 / 执行器  | Anthropic Python SDK       |
| OpenAI-compatible API                                            | 共享业务工具       | Anthropic /v1/messages     |

Claude 的 tool_use → tool_result 多轮链路应保持 Anthropic 原生 content block，不在 smolagents 内部重新序列化为 OpenAI tool call 或纯文本观察。公共 Tool Registry 只复用“工具定义和执行逻辑”，不强制复用同一种模型协议。

# 4. Provider 接入规范

## 4.1 OpenAI 路径

- smolagents 使用 OpenAIModel 连接现有 OpenAI-compatible API server。

- 如果直接调用 OpenAI 官方 API，优先使用官方 openai-python，并在需要
reasoning / tool calling / 多轮工作流时采用 Responses API。

- 思考深度参数必须按实际 endpoint 映射：Responses API 使用
reasoning.effort；Chat Completions/部分兼容服务通常使用 reasoning_effort。

- 兼容网关必须明确验证是否透传 reasoning 参数；“兼容 OpenAI
基础消息格式”不等于支持 reasoning 控制。

## 4.2 Claude 路径

- 使用 Anthropic Python SDK，base_url 指向自建 Anthropic-compatible
网关。

- 调用协议固定为 POST /v1/messages，不让 Claude 走
/v1/chat/completions。

- 新一代 Claude 采用 adaptive thinking + output_config.effort
控制推理投入；旧一代 extended-thinking 模型按能力使用 budget_tokens。

- 流式输出、tool_use/tool_result、thinking block、prompt caching 等保持
Anthropic 原生格式。

# 5. 统一推理深度（Reasoning）设计

上层只暴露一个统一参数 reasoning_level；Router 根据 provider、endpoint 和 model capability 转换为各自原生参数。reasoning_level 是“行为级意图”，不是严格等价的 token 数。

| **统一级别** | **OpenAI 映射**           | **Claude 映射**                       | **建议用途**             |
|--------------|---------------------------|---------------------------------------|--------------------------|
| off          | none（仅支持该值的模型）  | thinking disabled（仅支持关闭的模型） | 最低延迟、简单任务       |
| low          | reasoning effort = low    | adaptive + effort = low               | 高频简单 Agent 步骤      |
| medium       | reasoning effort = medium | adaptive + effort = medium            | 默认平衡档               |
| high         | reasoning effort = high   | adaptive + effort = high              | 复杂推理、编码、工具任务 |
| xhigh        | 模型支持时 xhigh          | 模型支持时 xhigh，否则降级 high       | 长周期 Agent / 高难任务  |
| max          | 模型支持时 max            | 模型支持时 max，否则降级 xhigh/high   | 能力优先、成本与延迟次要 |

> **模型能力必须动态校验**
>
> 不要假设所有 OpenAI 或 Claude 型号都支持相同 effort 档位。Router 应维护 model capability 表：支持的 reasoning levels、是否允许关闭 thinking、是否支持 adaptive、是否支持固定 budget_tokens。遇到不支持的档位时，应按配置选择“明确报错”或“有日志的降级”，不能静默改变行为。

## 5.1 当前建议默认值

| **场景**                | **reasoning_level** | **说明**                            |
|-------------------------|---------------------|-------------------------------------|
| 普通问答 / 简单工具调用 | low                 | 优先降低 TTFT、token 和工具调用次数 |
| 通用 Agent 默认         | medium              | 作为质量/延迟平衡起点               |
| 复杂代码 / 多工具规划   | high                | 质量优先                            |
| 长周期复杂 Agent        | xhigh（能力允许时） | 仅在评测证明收益后启用              |

Claude 官方当前将 effort 视为软控制信号；在 adaptive thinking 下，低 effort 可能对简单请求完全不触发 thinking。对于依赖 prompt caching 的长会话，建议在同一会话内保持 effort 稳定，因为改变 effort 可能影响缓存命中。

# 6. 运行时可靠性与超时策略

| **参数**                | **建议默认**         | **说明**                                     |
|-------------------------|----------------------|----------------------------------------------|
| streaming               | true                 | OpenAI/Claude 长响应都优先流式，避免整段等待 |
| OpenAI request timeout  | 300 s                | 建议起点，按业务 SLA 调整                    |
| Claude request timeout  | 600 s                | 复杂 thinking / Agent 任务预留更长时间       |
| max_retries（排障）     | 0                    | 先排除重复等待导致的假性超长                 |
| max_retries（稳定后）   | 1                    | 仅对可重试网络/限流错误启用                  |
| fallback                | 排障阶段关闭         | 避免一次失败被串行放大成多次长等待           |
| proxy / LB idle timeout | > 首 token 最坏耗时 | Nginx/ALB/Ingress 必须与流式策略一致         |

监控至少记录：provider、model_id、reasoning_level、endpoint、请求总时长、TTFT（Time To First Token）、输出持续时间、重试次数、状态码、stop_reason、input/output tokens。只有区分 TTFT 与生成时间，才能判断是模型慢、Gateway 慢还是代理超时。

# 7. 模块边界与配置项

| **模块**                   | **职责**                                      | **关键输入/输出**                           |
|----------------------------|-----------------------------------------------|---------------------------------------------|
| Model Router               | 选择 OpenAI / Claude 路径；能力校验；参数映射 | provider、model_id、reasoning_level         |
| OpenAI Agent Engine        | 运行现有 smolagents Agent                     | OpenAI-compatible messages/tools            |
| Claude Native Agent Engine | 维护 Anthropic 原生 Messages / tool loop      | thinking、tool_use、tool_result、raw blocks |
| Tool Registry              | 共享业务工具定义与执行                        | tool schema、executor、result               |
| Runtime Policy             | 超时、重试、流式、日志、限流                  | timeout、retry、SSE、metrics                |
| Capability Registry        | 记录不同模型支持的 reasoning / native feature | model_id → capability set                   |

## 7.1 建议统一配置字段

- provider：openai \| anthropic

- model_id：实际模型标识；不要把能力写死在 provider 名上

- reasoning_level：off \| low \| medium \| high \| xhigh \| max

- streaming：默认 true

- request_timeout_s：OpenAI 建议 300；Claude 建议 600

- max_retries：排障 0，稳定后建议 1

- max_output_tokens：按模型和任务类型配置，不设全局固定死值

- native_options：仅用于 Provider 原生扩展字段；不得跨 Provider 误传

# 8. 从 LiteLLM 迁移的实施步骤

1.  建立 Model Router 和 Capability Registry，但暂时不改变业务 Agent
调用入口。

2.  OpenAI 路径改为 smolagents OpenAIModel 直连现有 OpenAI-compatible
API，关闭 LiteLLM SDK/Proxy。

3.  Claude 路径改为 Anthropic SDK 直连自建 /v1/messages，并保留原生
content blocks。

4.  接入统一 reasoning_level 映射；先只开放 low / medium / high
三档，验证稳定后再开放 xhigh / max。

5.  打开 streaming；统一检查
Backend、Nginx/Ingress、负载均衡器和客户端超时。

6.  先以 max_retries=0 做对照测试，记录 TTFT/总耗时/错误位置；稳定后改为
1。

7.  完成 Claude 原生能力验收后再移除旧 LiteLLM 配置和依赖。

# 9. 验收标准

| **验收项**          | **通过条件**                                                           |
|---------------------|------------------------------------------------------------------------|
| OpenAI 基础调用     | smolagents 直连 OpenAI-compatible API 成功，工具调用正常               |
| OpenAI reasoning    | 不同 reasoning_level 能被网关正确透传，且耗时/token 有可观测差异       |
| Claude /v1/messages | 不经过 OpenAI chat/completions 与 LiteLLM                              |
| Claude thinking     | 支持的模型可按 effort 控制；不支持时有明确 capability 处理             |
| Claude tools        | tool_use → tool_result 原生多轮链路保持完整                            |
| Claude streaming    | 长任务连续收到 SSE/流式事件，无固定 30/60/120 秒中断                   |
| Prompt caching      | 需要时 cache_control 能透传，并可在 usage/日志观察命中行为             |
| 自建网关兼容性      | thinking、effort、tools、stream、usage、stop_reason 等关键字段不被丢弃 |
| 故障定位            | 日志可区分 client / backend / gateway / provider 超时                  |

# 10. 风险与控制

| **风险**                         | **控制策略**                                                         |
|----------------------------------|----------------------------------------------------------------------|
| 自建 Claude 网关只实现“基础兼容” | 对 /v1/messages 原生字段做能力测试；不通过则不能宣称 Claude 完整能力 |
| reasoning 档位随模型版本变化     | Capability Registry 按 model_id 管理；升级模型时重新验证             |
| 低 effort 质量下降               | 对业务任务建立固定 eval 集，比较质量、TTFT、token、工具成功率        |
| 高 effort 延迟/成本过大          | 默认 medium；复杂任务动态提升；设置 SLA 和 token 上限                |
| 同会话频繁改变 Claude effort     | 依赖 prompt caching 时保持会话内 effort 稳定                         |
| 重试放大总耗时                   | 排障阶段 retry=0；生产最多 1 次且仅对可重试错误                      |
| smolagents API 变化              | 固定依赖版本并在升级前运行 Agent/tool 回归测试                       |

# 11. 官方 GitHub 与文档原地址

以下地址作为实现和升级时的唯一优先参考源。GitHub 用于 SDK/框架源码与版本跟踪，官方文档用于 API 参数和模型能力确认。

## 11.1 GitHub

- **Hugging Face
smolagents：**[https://github.com/huggingface/smolagents](https://github.com/huggingface/smolagents)

- **OpenAI Python
SDK：**[https://github.com/openai/openai-python](https://github.com/openai/openai-python)

- **Anthropic Python
SDK：**[https://github.com/anthropics/anthropic-sdk-python](https://github.com/anthropics/anthropic-sdk-python)

## 11.2 官方文档

- **smolagents Models
文档：**[https://huggingface.co/docs/smolagents/reference/models](https://huggingface.co/docs/smolagents/reference/models)

- **OpenAI 最新模型 / reasoning
指南：**[https://developers.openai.com/api/docs/guides/latest-model](https://developers.openai.com/api/docs/guides/latest-model)

- **OpenAI Responses
API：**[https://platform.openai.com/docs/api-reference/responses](https://platform.openai.com/docs/api-reference/responses)

- **Claude
Effort：**[https://platform.claude.com/docs/en/build-with-claude/effort](https://platform.claude.com/docs/en/build-with-claude/effort)

- **Claude Adaptive
Thinking：**[https://platform.claude.com/docs/en/build-with-claude/adaptive-thinking](https://platform.claude.com/docs/en/build-with-claude/adaptive-thinking)

- **Claude Extended
Thinking（旧模型/迁移参考）：**[https://platform.claude.com/docs/en/build-with-claude/extended-thinking](https://platform.claude.com/docs/en/build-with-claude/extended-thinking)

> **实施时的版本原则**
>
> OpenAI 与 Claude 的 reasoning/effort 支持档位是“按模型变化”的能力，不应写死在框架代码中。每次切换模型版本时，先查询对应官方 model page，再更新 Capability Registry 和回归测试。

# 12. 最终一致性结论

本方案满足当前目标：保留 smolagents 作为现有 OpenAI Agent 框架；Claude 不经过 OpenAI-compatible 转换和 LiteLLM，使用 Anthropic 原生 Messages API；OpenAI 与 Claude 均可由统一 reasoning_level 控制，但实际参数由 Provider/模型能力映射；长响应采用 streaming、显式 timeout 和受控 retry。

优先实施顺序固定为：先移除 LiteLLM 并建立双协议直连 → 再统一 reasoning 配置 → 再做 Claude 原生高级能力与性能调优。无需为了接口形式统一而牺牲 Claude 原生能力。
