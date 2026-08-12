# smolagents 长流式中断快速重连改造方案

**版本：v2.0**  
**适用框架：smolagents 1.26.0**  
**适用场景：OpenAI / Anthropic 双协议，多 Agent、CodeAgent、长 reasoning、长流式输出**  
**目标：解决长流式响应中途被网关掐断后，`AgentGenerationError` 冒泡并触发整个 `CodeAgent.run()` 重跑的问题。**

---

## 1. 根因结论

当前问题不是普通的“SDK 建连失败”，而是：

```text
CodeAgent Step N
    ↓
LLM 长流式请求
    ↓
reasoning=max，持续数分钟
    ↓
SSE 已经开始并持续返回 token
    ↓
网关出现 HTTP/2 RST / Upstream failed / stream failed
    ↓
stream iterator 中途抛异常
    ↓
smolagents 包装为 AgentGenerationError
    ↓
异常冒泡到 run_agent_stage
    ↓
整个 CodeAgent.run() 重跑
    ↓
重新请求、重新 reasoning
    ↓
出现 20 秒～数分钟的空窗
```

关键问题：

> **Retry 边界放错层了。**

当前恢复粒度是：

```text
Stage / CodeAgent.run 级别
```

而正确恢复粒度应该是：

```text
当前 logical model call 级别
```

---

# 2. 原方案保留与新增内容

原有：

```text
smolagents_openai_claude_reasoning_plan.md
```

继续保留，它负责：

- OpenAI / Anthropic 双协议接入
- `reasoning_level` 六档映射
- `model_capabilities.py`
- `model_router.py`
- 自定义 `AnthropicModel`
- provider 显式选择
- timeout 基础配置
- LiteLLM 移除

该方案不需要推翻。

新增本方案，专门负责：

```text
mid-stream interruption
fast reconnect
atomic buffering
logical-call replay
retry ownership
```

---

# 3. 改造后的模型调用链

原来：

```text
CodeAgent
   ↓
OpenAIModel / AnthropicModel
   ↓
SDK
   ↓
Gateway
```

改成：

```text
CodeAgent
   ↓
ResilientModel
   ↓
OpenAIModel / AnthropicModel
   ↓
SDK
   ↓
Gateway
```

最终结构：

```text
                    smolagents.CodeAgent
                             │
                             ▼
                  ┌───────────────────┐
                  │  ResilientModel   │
                  │                   │
                  │ fast retry        │
                  │ stream buffering  │
                  │ replay            │
                  │ error classify    │
                  │ telemetry         │
                  └─────────┬─────────┘
                            │
               ┌────────────┴────────────┐
               ▼                         ▼
       smolagents.OpenAIModel      AnthropicModel
               │                         │
         OpenAI SDK               Anthropic SDK
        max_retries=0             max_retries=0
               │                         │
               └────────────┬────────────┘
                            ▼
                          Gateway
```

---

# 4. 新增文件

新增：

```text
llm/
├── resilient_model.py
└── retry_policy.py
```

现有：

```text
model_router.py
anthropic_model.py
model_capabilities.py
```

继续保留。

推荐结构：

```text
project/
├── agents/
├── llm/
│   ├── __init__.py
│   ├── anthropic_model.py
│   ├── resilient_model.py
│   └── retry_policy.py
├── model_router.py
├── model_capabilities.py
├── config.py
└── ...
```

---

# 5. Retry Ownership

必须明确只有一层负责主要网络 retry。

推荐：

```text
OpenAI SDK retry        = OFF
Anthropic SDK retry     = OFF
smolagents retry        = OFF / 不作为网络恢复
ResilientModel retry    = ON
run_agent_stage retry   = 最后兜底
checkpoint/resume       = 保留
```

即：

```text
SDK max_retries = 0
```

原因：

如果同时开启：

```text
SDK retry
×
ResilientModel retry
×
run_agent_stage retry
```

会产生多层 retry amplification，导致：

```text
用户看到分钟级空窗
但无法判断到底卡在哪一层
```

---

# 6. 核心原则：一个 Logical Call，多个 Network Attempts

例如：

```text
CodeAgent Step 4
    ↓
logical_call_id = call_0027
    │
    ├─ attempt 1 → stream 中断
    ├─ attempt 2 → 503
    └─ attempt 3 → success
```

对 CodeAgent 来说：

```text
仍然只是 Step 4 的一次 model call
```

必须保证：

- 不增加 `step_number`
- 不消耗额外 `max_steps`
- 不写入 Agent memory
- 不产生新的 reasoning step
- 不重复执行 tool
- 不触发整个 `CodeAgent.run()` 重启

---

# 7. 最重要修改：`generate_stream()` 使用 Atomic Buffer

这是 P0。

## 7.1 现状问题

错误路径：

```text
Provider SSE
   ↓
token
   ↓
token
   ↓
直接 yield 给 CodeAgent
   ↓
已经收到 10000 字
   ↓
HTTP/2 RST
   ↓
AgentGenerationError
```

如果 partial 内容已经交给 CodeAgent，再 replay 时无法保证一致性。

尤其 `CodeAgent` 输出可能包含：

```python
result = some_tool(...)
```

如果只收到半截 Python action，就存在执行风险。

---

## 7.2 修改后

```text
Provider SSE
   ↓
ResilientModel attempt-local buffer
   ↓
持续接收所有 delta
   ↓
确认完整结束
   ↓
才把完整结果交给 CodeAgent
```

如果中途失败：

```text
attempt 1
   ↓
已收到 1215 / 5000 / 10000 字
   ↓
HTTP/2 RST
   ↓
partial buffer 全部丢弃
   ↓
快速重连
   ↓
attempt 2
   ↓
完整 replay 相同 logical request
```

只有完整成功：

```text
stream complete
```

才允许向上层返回。

---

# 8. `try/except` 必须包住整个 Stream Iteration

错误写法：

```text
try:
    stream = client.xxx.create(stream=True)
except:
    retry
```

这只能捕获：

```text
create() 阶段失败
```

但当前问题发生在：

```text
stream = create()   # 成功

for event in stream:
    ...
    ...
    # 几分钟后这里才抛异常
```

因此必须：

```text
try:
    创建 stream
    消费整个 stream
    写入临时 buffer
    验证正常结束
except transient_stream_error:
    丢弃 buffer
    retry 整个 logical call
```

这是本方案最重要的实现检查点。

---

# 9. Mid-stream Failure 默认策略

固定：

```text
STREAM_RECOVERY_POLICY = full_replay
```

流程：

```text
stream 中途断开
    ↓
discard partial
    ↓
重新建立 HTTP/SSE
    ↓
发送相同 messages
    ↓
重新生成完整响应
```

第一版不要做：

```text
partial continuation
```

原因：

- CodeAgent 输出可能是 Python
- code fence 可能未闭合
- continuation 可能重复 token
- reasoning / code 结构可能改变
- 不容易保证执行安全

因此：

> **CodeAgent 场景优先完整 replay，不拼接半截输出。**

---

# 10. Retry 次数

建议默认：

```text
LLM_RETRY_MAX_ATTEMPTS = 3
```

含义：

```text
attempt 1 = 正常调用
attempt 2 = 第一次 retry
attempt 3 = 第二次 retry
```

总共最多 3 次。

---

# 11. 快速 Backoff

当前目标是“断了快速重连”，不要用很长退避。

建议：

```text
attempt 1 fail
    ↓
等待 0.8s ±25% jitter

attempt 2 fail
    ↓
等待 2.0s ±25% jitter

attempt 3
```

大致范围：

```text
第一次：
0.6 ~ 1.0s

第二次：
1.5 ~ 2.5s
```

目的：

- 快速恢复短暂网关抖动
- 避免多个 worker 同时重试形成 retry storm
- 避免几十秒到几分钟的不可解释等待

---

# 12. Retry-After

如果服务端返回：

```text
Retry-After
```

优先参考。

建议：

```text
LLM_FAST_RETRY_AFTER_CAP = 15s
```

如果服务端要求等待超过 15 秒：

```text
不继续占用 fast retry
```

应交给：

```text
run_agent_stage / checkpoint / task scheduler
```

做长时间恢复。

---

# 13. Timeout 分层

不要只使用：

```text
timeout = 300
```

或：

```text
timeout = 600
```

控制所有阶段。

应该拆成：

- connect
- read
- write
- pool

---

## 13.1 OpenAI 建议默认值

```text
connect_timeout = 8s
read_timeout    = 300s
write_timeout   = 60s
pool_timeout    = 10s
```

---

## 13.2 Anthropic 建议默认值

```text
connect_timeout = 8s
read_timeout    = 600s
write_timeout   = 60s
pool_timeout    = 10s
```

逻辑：

```text
连接建立不了
→ 8 秒左右快速失败
→ 马上 retry

连接已经建立
→ reasoning max 可以等待几分钟
```

---

# 14. Retry Error Matrix

## 14.1 必须 Retry

连接类：

```text
ConnectionError
ConnectionResetError
ConnectTimeout
ReadTimeout
RemoteProtocolError
ReadError
unexpected EOF
connection closed
incomplete chunked read
HTTP/2 RST_STREAM
```

实际实现中优先匹配具体异常类型，不要只依赖字符串。

---

## 14.2 HTTP Retry

```text
408
409
429
500
502
503
504
Anthropic 529
```

---

## 14.3 实测 Gateway 错误

对当前网关，应识别：

```text
Upstream service temporarily unavailable
Upstream request failed
Upstream HTTP/2 stream failed
```

如果这些最终被包装进：

```text
AgentGenerationError
```

需要在更底层的 Model / stream consumption 位置捕获原始 transport error，避免等它已经冒泡到 smolagents Agent 层再处理。

---

# 15. 不 Retry 的错误

```text
400
401
402
403
404
413
422
```

理由：

```text
400 → 请求构造错误
401 → API key / auth 错
403 → 权限错误
404 → endpoint / model 错
413 → payload 太大
422 → 参数不合法
```

这些错误重复请求不会自行恢复。

---

# 16. Client 必须复用

不要：

```text
每个 model call
→ new OpenAI()
→ new Anthropic()
```

推荐：

```text
Model 初始化
    ↓
创建 client 一次
    ↓
self.client
    ↓
连续复用
```

例如：

```text
Worker 生命周期

client
 ├─ call 1
 ├─ call 2
 ├─ call 3
 └─ call N
```

目的：

- 复用 connection pool
- 减少 TCP/TLS 建连
- 减少本机/服务器网络抖动
- 提高连续调用稳定性

---

# 17. `model_router.py` 修改

原来：

```text
provider
 ↓
OpenAIModel / AnthropicModel
 ↓
return
```

改成：

```text
provider
 ↓
创建 base_model
 ↓
ResilientModel(base_model)
 ↓
return
```

例如逻辑：

```text
provider=openai
    ↓
OpenAIModel
    ↓
ResilientModel

provider=anthropic
    ↓
AnthropicModel
    ↓
ResilientModel
```

Agent 层无感。

---

# 18. `anthropic_model.py` 修改边界

继续负责：

- `/v1/messages` 协议
- messages 转换
- system 提取
- stop sequences
- reasoning/thinking 映射
- `ChatMessage`
- `ChatMessageStreamDelta`
- usage
- client 创建

新增要求：

```text
Anthropic SDK max_retries = 0
```

并使用 granular timeout。

不要把通用 retry policy 再重复写在 `AnthropicModel` 内。

---

# 19. OpenAIModel 修改边界

继续使用：

```text
smolagents.OpenAIModel
```

不 fork smolagents。

确保底层 SDK：

```text
max_retries = 0
```

并配置 timeout。

通用 retry 全部交给：

```text
ResilientModel
```

---

# 20. `run_agent_stage` 修改

当前问题之一：

```text
AgentGenerationError
    ↓
run_agent_stage retry
    ↓
整个 CodeAgent.run() 重跑
```

改造后：

```text
普通网络错误
    ↓
ResilientModel 内吸收
```

只有：

```text
attempt 1 fail
attempt 2 fail
attempt 3 fail
```

才向上抛：

```text
TransientModelUnavailable
```

此时：

```text
run_agent_stage
```

只做最后兜底。

建议：

```text
stage-level retry 最多 1 次
```

或者：

```text
保存 checkpoint
→ WAITING_RETRY
→ 30~60 秒后恢复
```

不要让普通网络抖动频繁触发：

```text
整个 CodeAgent.run()
```

---

# 21. 自定义异常

建议新增统一异常：

```text
TransientModelUnavailable
```

字段：

```text
logical_call_id
provider
model
attempts
last_error_type
last_status_code
elapsed_seconds
```

不要把多个底层 traceback 塞进 Agent prompt。

---

# 22. 网络错误不能进入 Agent Memory

必须保证以下内容不进入：

```text
memory.steps
```

例如：

```text
503
Upstream request failed
RemoteProtocolError
ConnectionResetError
ReadTimeout
```

原因：

网络属于：

```text
transport concern
```

不是：

```text
Agent world state
```

否则模型下一步可能错误 reasoning：

```text
“上一轮出现 HTTP 503，我应该……”
```

---

# 23. 不允许执行 Partial Tool Action

必须满足：

```text
Model stream
 ↓
完整成功
 ↓
CodeAgent parse
 ↓
Python action
 ↓
Tool execute once
```

禁止：

```text
stream partial
 ↓
已经执行 tool
 ↓
网络失败
 ↓
retry
 ↓
同一个 tool 再执行一次
```

这也是为什么 retry 必须放在：

```text
Model → CodeAgent
```

边界以内。

---

# 24. Logging

每次 logical call 记录：

```text
timestamp
logical_call_id
attempt
provider
model
agent_name
step_number
event
elapsed_seconds
```

建议 event：

```text
MODEL_CALL_START
ATTEMPT_START
HTTP_CONNECTED
FIRST_STREAM_EVENT
STREAM_INTERRUPTED
ATTEMPT_FAILED
RETRY_SLEEP
ATTEMPT_SUCCESS
MODEL_CALL_SUCCESS
MODEL_CALL_FAILED
```

---

# 25. 推荐日志示例

```text
20:11:01
call=018
agent=review
step=3
attempt=1
event=ATTEMPT_START

20:11:02
event=HTTP_CONNECTED

20:11:08
event=FIRST_STREAM_EVENT

20:14:33
event=STREAM_INTERRUPTED
error=RemoteProtocolError
received_chars=11842

20:14:34
call=018
attempt=2
event=ATTEMPT_START

20:17:05
event=ATTEMPT_SUCCESS

20:17:05
call=018
event=MODEL_CALL_SUCCESS
```

用户只需要看到：

```text
正在生成……
```

---

# 26. 日志隐私

默认不要记录：

```text
API key
完整 prompt
完整 idea.md
完整 data
完整论文输出
```

可以记录：

```text
input_tokens
output_tokens
payload_bytes
received_chars
request_id
message_hash
elapsed
```

---

# 27. Retry 与 Reasoning

Retry 期间必须保持相同：

```text
provider
model
messages
reasoning_level
stop sequences
其他 generation 参数
```

不得自动：

```text
max → high
Claude → OpenAI
```

第一版只做：

```text
same logical request replay
```

---

# 28. Retry 与 `max_steps`

必须保证：

```text
Step 3
   ↓
attempt 1 fail
attempt 2 fail
attempt 3 success
```

仍然只算：

```text
Step 3
```

网络 attempt 不是 Agent step。

---

# 29. Retry 与用户取消

如果用户主动：

```text
Cancel
```

必须立即停止 retry。

不能：

```text
用户取消
↓
ResilientModel 仍继续 attempt 2 / 3
```

---

# 30. 推荐配置

```text
# ========================
# Resilient Model
# ========================

LLM_RETRY_ENABLED=true

LLM_RETRY_MAX_ATTEMPTS=3

LLM_RETRY_DELAY_1=0.8
LLM_RETRY_DELAY_2=2.0

LLM_RETRY_JITTER_RATIO=0.25

LLM_RESPECT_RETRY_AFTER=true
LLM_FAST_RETRY_AFTER_CAP=15

# ========================
# Streaming
# ========================

LLM_ATOMIC_STREAM_BUFFER=true
LLM_STREAM_RECOVERY_POLICY=full_replay

# ========================
# SDK Retry
# ========================

OPENAI_SDK_MAX_RETRIES=0
ANTHROPIC_SDK_MAX_RETRIES=0

# ========================
# OpenAI Timeout
# ========================

OPENAI_CONNECT_TIMEOUT=8
OPENAI_READ_TIMEOUT=300
OPENAI_WRITE_TIMEOUT=60
OPENAI_POOL_TIMEOUT=10

# ========================
# Anthropic Timeout
# ========================

ANTHROPIC_CONNECT_TIMEOUT=8
ANTHROPIC_READ_TIMEOUT=600
ANTHROPIC_WRITE_TIMEOUT=60
ANTHROPIC_POOL_TIMEOUT=10
```

以上属于：

```text
建议默认值 / 可配置参数
```

---

# 31. 测试计划

## T1：正常调用

```text
attempt 1 success
```

要求：

- provider 只调用 1 次
- 正常返回
- 无 retry

---

## T2：503 → Success

```text
attempt 1 = 503
attempt 2 = success
```

要求：

- CodeAgent 只看到成功结果
- memory 不包含 503
- step_number 不增加

---

## T3：Connection Reset → Success

同 T2。

---

## T4：Mid-stream interruption

模拟：

```text
delta 1
delta 2
delta 3
HTTP/2 RST
```

然后：

```text
attempt 2 complete
```

要求：

- attempt 1 partial delta 一个都不能泄漏给 CodeAgent
- CodeAgent 最终只收到 attempt 2 完整结果

这是 P0 测试。

---

## T5：三次全部失败

```text
attempt 1 fail
attempt 2 fail
attempt 3 fail
```

要求：

```text
raise TransientModelUnavailable
```

---

## T6：401

要求：

```text
attempt count = 1
```

不 retry。

---

## T7：413 / 422

要求：

立即失败。

---

## T8：429 + Retry-After

要求：

```text
Retry-After <= cap
→ retry

Retry-After > cap
→ fast retry 不长期等待
→ 交给上层恢复
```

---

## T9：Tool 不重复执行

模拟：

```text
attempt 1 stream interrupted
attempt 2 返回完整 Python action
```

要求：

```text
tool execution count = 1
```

---

## T10：Client 复用

连续多个 model call。

要求：

```text
OpenAI client 创建 1 次 / model 生命周期
Anthropic client 创建 1 次 / model 生命周期
```

---

## T11：Regression

现有：

- Draft
- Review
- Manager
- evidence
- outline
- orchestrator

测试全部继续通过。

---

# 32. 验收标准

必须全部满足：

```text
AC1  503 后可快速自动恢复
AC2  HTTP/2 stream reset 可在当前 model call 内恢复
AC3  partial action 永不执行
AC4  网络 retry 不进入 Agent memory
AC5  网络 retry 不增加 max_steps
AC6  tool 不重复执行
AC7  永久错误不进行无意义 retry
AC8  run_agent_stage 不再承担普通网络波动恢复
AC9  smolagents 源码不修改
AC10 Draft / Review / Manager 行为不改变
```

---

# 33. 优先级

## P0

```text
1. generate_stream() 捕获 mid-stream error
2. atomic buffering
3. full replay 当前 logical call
4. retry 不越过 Model boundary
5. partial output 不得泄漏给 CodeAgent
```

## P1

```text
6. fast backoff
7. granular timeout
8. client reuse
9. structured retry logging
```

## P2

```text
10. stage checkpoint
11. WAITING_RETRY
12. circuit breaker
```

---

# 34. 开发 Agent 可直接执行的修改指令

```text
在现有 smolagents 1.26.0 架构上修复 mid-stream API interruption。

不要修改：
- Draft Agent
- Review Agent
- Manager Agent
- prompts
- tools
- max_steps
- evidence workflow
- 章节业务逻辑
- smolagents site-packages 源码

新增：
- llm/resilient_model.py
- llm/retry_policy.py

修改：
- model_router.py
- anthropic_model.py 的 client 配置
- config.py / .env.example
- model-related tests
- run_agent_stage 的网络兜底策略

要求：

1. model_router.py 创建 OpenAIModel / AnthropicModel 后，
   统一包装成 ResilientModel(base_model) 再交给 CodeAgent。

2. OpenAI SDK 与 Anthropic SDK 显式 max_retries=0。

3. ResilientModel 是唯一 transient retry owner。

4. 一个 logical model call 最多总共 3 attempts。

5. attempt 1 失败后等待 0.8s ±25% jitter。

6. attempt 2 失败后等待 2.0s ±25% jitter。

7. generate_stream() 必须实现 atomic buffering。

8. provider stream 完整结束前，
   不允许把 partial delta 直接 yield 给 CodeAgent。

9. try/except 必须覆盖整个 stream iteration，
   不能只覆盖 client.xxx.create()。

10. 如果流已经输出部分内容后发生：
    - HTTP/2 RST
    - Upstream request failed
    - Upstream HTTP/2 stream failed
    - RemoteProtocolError
    - ConnectionResetError
    - ReadError
    - unexpected EOF
    - stream closed

    必须：
    - discard 当前 attempt 的全部 partial buffer
    - 快速重新建立连接
    - replay 相同 logical request

11. Retry：
    - 408
    - 409
    - 429
    - 500
    - 502
    - 503
    - 504
    - Anthropic 529
    - ConnectionError
    - ConnectionReset
    - ConnectTimeout
    - ReadTimeout
    - RemoteProtocolError
    - mid-stream transport interruption

12. 不 Retry：
    - 400
    - 401
    - 402
    - 403
    - 404
    - 413
    - 422

13. connect timeout：
    OpenAI = 8s
    Anthropic = 8s

14. read timeout：
    OpenAI = 300s
    Anthropic = 600s

15. provider client 在 Model 生命周期内复用，
    不允许每个 call 重新创建。

16. transient retry 不能增加 CodeAgent step_number。

17. transient network error 不能写入 Agent memory。

18. partial Python action 不能执行。

19. tool 只能在完整成功 model output 返回后执行一次。

20. 增加 logical_call_id + attempt 日志。

21. 日志不得记录 API key、完整 prompt、完整实验数据。

22. ResilientModel 三次 fast retry 全失败后，
    才抛 TransientModelUnavailable。

23. run_agent_stage 对 TransientModelUnavailable
    只做最后兜底，不再把普通 API 抖动当作整个 Agent 重跑的主要恢复机制。

24. 添加测试：
    - 503 → success
    - connection reset → success
    - mid-stream interruption → success
    - 401 不 retry
    - 3 attempts 全失败
    - tool 不重复执行
    - max_steps 不因 retry 消耗
    - Agent memory 不包含 transient error
    - client 复用
    - existing regression tests

25. 不要做：
    - Prompt 优化
    - reasoning level 调整
    - provider fallback
    - LiteLLM 恢复
    - smolagents 源码修改
```

---

# 35. 最终目标行为

改造前：

```text
20:00 LLM 请求开始
20:03 HTTP/2 stream failed
20:03 AgentGenerationError
20:04 run_agent_stage 重跑整个 CodeAgent
20:08 下一轮又失败
```

改造后：

```text
20:00 logical_call=18 attempt=1
20:03 STREAM_INTERRUPTED
20:03:01 attempt=2
20:05 SUCCESS
```

CodeAgent 只看到：

```text
Step N 成功
```

用户体验：

```text
网关偶尔抖动
    ↓
模型调用短暂停顿
    ↓
自动恢复
    ↓
论文流水线继续运行
```

而不是：

```text
网关偶尔抖动
    ↓
整个 Agent 重启
    ↓
重新 reasoning 数分钟
    ↓
产生长时间空窗
```

---

# 36. 最终原则

最终只记住 6 条：

```text
1. SDK retry 关闭，避免多层 retry。
2. ResilientModel 是唯一快速 retry owner。
3. try/except 覆盖整个 stream consumption。
4. SSE partial 必须 atomic buffer，失败则 discard + full replay。
5. 网络 retry 不得进入 Agent step / memory。
6. 真正持续性故障才交给 stage checkpoint/resume。
```

这就是当前问题的正式修复方案。
