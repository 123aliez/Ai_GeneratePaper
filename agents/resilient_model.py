"""ResilientModel：在 Model 层吃掉瞬时网络错误，把 retry 边界下沉到"单次 logical call"。

为什么存在
----------
网关对 reasoning=max 的长流式响应不稳：单次响应几分钟，期间任意 HTTP/2 RST /
``Upstream request failed`` 都会把流式中途掐断。openai / anthropic SDK 的
``max_retries`` 只覆盖**连接级**失败，**不覆盖"流式已开始但中途断开"**。那种半截流
被 smolagents 包成 ``AgentGenerationError``，冒泡到 ``run_agent_stage`` 触发整个
``CodeAgent.run()`` 重跑，每次重跑几分钟——串行几十次请求后表现为"分钟级空窗"。

本类把 retry 收拢到唯一一层（ResilientModel），SDK / smolagents / run_agent_stage
三层都不再做网络重试：

    CodeAgent._step_stream
        ↓ generate_stream()
    ResilientModel.generate_stream()    ← 唯一瞬时重试 owner
        ├─ attempt 1: 收到 partial → 流中断 → 丢弃
        ├─ attempt 2: 503 → 重发
        └─ attempt 3: 完整成功 → 才一次性把 delta 流 yield 给上层
        ↓
    OpenAIModel / AnthropicModel (max_retries=0)

核心契约（midstream plan v2 §6/§7/§23）
    - 一个 logical call 最多 ``max_attempts`` 次 network attempt；
    - **partial delta 绝不泄漏给 CodeAgent**——流中断就把本次 buffer 全丢，重发；
    - retry 不增加 CodeAgent 的 ``step_number``、不进 Agent memory、不重复执行 tool
      （因为 tool 执行发生在 CodeAgent 拿到完整 model output 之后，retry 在那之前）；
    - 永久错误（401/413/422）不重试，原样抛；
    - 重试到上限仍失败 → 抛 ``TransientModelUnavailable``，交给 ``run_agent_stage``
      做最后一次 stage 级兜底。

Atomic buffering 的取舍
    成功路径下，partial delta 不再逐字 yield（无法实时心跳）。代价用 ResilientModel
    自己打的 attempt/retry 日志补回可见性（orchestrator 的 ``print_stream_progress``
    收到的就是一份完整内容，照常渲染进度行）。
"""
from __future__ import annotations

import time
from typing import Any, Generator

from smolagents.models import (
    ChatMessage,
    ChatMessageStreamDelta,
    Model,
)

from . import retry_policy
from .retry_policy import (
    ErrorVerdict,
    RetrySchedule,
    TransientModelUnavailable,
)


class ResilientModel(Model):
    """包裹任意 smolagents ``Model``，提供"单次 logical call 级"的瞬时重试。

    Parameters
    ----------
    base_model : Model
        真正打 SDK 的底层 Model（OpenAIModel / AnthropicModel）。其 SDK ``max_retries``
        应为 0——瞬时重试全部由本类负责，避免多层 amplification。
    schedule : RetrySchedule
        重试计划（次数 / 退避 / jitter / retry-after cap）。来自 config。
    owner : str
        终端日志的 owner 标签（Draft / Review / Manager），对齐 orchestrator 的
        ``[Owner] action | ...`` 风格。
    """

    def __init__(self, base_model: Model, schedule: RetrySchedule, owner: str = "Agent",
                 provider: str = ""):
        # 不调 super().__init__ 的全套：base_model 已是合法 Model，我们只需把它的
        # 对外属性（model_id / kwargs 等）透传给 CodeAgent。直接继承 Model 并补齐
        # CodeAgent 实际读取的几个属性即可。
        Model.__init__(
            self,
            flatten_messages_as_text=getattr(base_model, "flatten_messages_as_text", False),
            tool_name_key=getattr(base_model, "tool_name_key", "name"),
            tool_arguments_key=getattr(base_model, "tool_arguments_key", "arguments"),
            model_id=getattr(base_model, "model_id", None),
        )
        self.base = base_model
        self.schedule = schedule
        self.owner = owner
        # provider 显式传入（router 知道是 openai/anthropic），用于 TransientModelUnavailable
        # 的诊断字段；透传不到 base，且 base.model_id 不等于 provider。
        self.provider = provider or type(base_model).__name__
        # 透传 base 的 kwargs（部分代码路径会读 model.kwargs，如 OpenAIModel 把
        # reasoning_effort 放在 kwargs 里）。本类自己不产生新 kwargs。
        self.kwargs = getattr(base_model, "kwargs", {})
        # client 复用：base_model 的 client 在其生命周期内只建一次，本类每次 attempt
        # 都复用同一个 client（不 new），由 base_model 负责。
        self._logical_call_counter = 0

    # ── 让本类"看起来像底层 Model"：CodeAgent 读的属性全透传 ────────────────
    def __getattr__(self, name: str) -> Any:
        # 仅在常规属性查找失败时兜底转发到 base_model（如 custom_role_conversions、
        # client、supports_stop_parameter 等）。__init__ 里显式设置的属性不会走到这。
        base = self.__dict__.get("base")
        if base is not None:
            return getattr(base, name)
        raise AttributeError(name)

    def to_dict(self) -> dict:  # 安全导出：不泄漏 base 的 api_key
        base = self.base.to_dict() if hasattr(self.base, "to_dict") else {}
        base = {k: v for k, v in base.items() if k != "api_key"}
        base["__resilient__"] = True
        base["max_attempts"] = self.schedule.max_attempts
        return base

    # ── 日志 ────────────────────────────────────────────────────────────────
    def _log(self, action: str, detail: str = "") -> None:
        # 直接写 sys.__stdout__（真实 stdout），不走 print()。原因：orchestrator 的
        # run_agent_stage 把 agent.run() 包在 contextlib.redirect_stdout(
        # ProgressFilteringStdout) 里，本类的诊断日志若走 print 会被重定向进进度过滤器，
        # 而 [Owner] model | ... 行不匹配过滤器的工具/阶段模式，会被静默丢弃——
        # 用户就看不到 retry 信号，会以为还是卡住。诊断日志必须始终可见。
        import sys
        stream = sys.__stdout__
        tag = f"[{self.owner:<8.8}]"
        line = f"{tag} {action:<11} | {detail}" if detail else f"{tag} {action:<11}"
        stream.write(line + "\n")
        stream.flush()

    def _next_call_id(self) -> int:
        self._logical_call_counter += 1
        return self._logical_call_counter

    # ── 共用的重试驱动 ─────────────────────────────────────────────────────
    def _retry_call(
        self,
        kind: str,                       # "generate" | "generate_stream"
        invoke,                          # callable(attempt) -> 结果（generate）或生成器工厂（stream）
        messages: list,
    ) -> Any:
        """驱动一次 logical call 的多次 attempt。

        - ``kind="generate"``：``invoke(attempt)`` 返回 ChatMessage，成功即返回。
        - ``kind="generate_stream"``：``invoke(attempt)`` 返回一个**新的**生成器，
          本方法在内部把每个 attempt 的 delta 累积进 attempt-local buffer；只要这个
          attempt 的流没完整结束就抛错，buffer 整个丢弃、重发。
          成功时把 buffer 里的 delta 按原顺序重新 yield 出去（atomic）。
        """
        call_id = self._next_call_id()
        max_attempts = self.schedule.max_attempts
        start = time.time()
        last_verdict: ErrorVerdict | None = None
        last_exc: BaseException | None = None
        attempts_made = 0  # 实际发起的 attempt 数（cap-hit 提前退出时 < max_attempts）

        for attempt in range(1, max_attempts + 1):
            attempts_made = attempt
            attempt_start = time.time()
            self._log("model", f"attempt {attempt}/{max_attempts} ({kind}) call={call_id}")
            buffer: list[ChatMessageStreamDelta] = []
            received_chars = 0
            try:
                if kind == "generate":
                    result = invoke(attempt)
                    elapsed = time.time() - attempt_start
                    self._log("model-ok", f"attempt {attempt} ok ({elapsed:.1f}s) call={call_id}")
                    return result
                # generate_stream：在 for 循环里消费底层生成器，遇到任何异常都
                # 视为"这次 attempt 失败"，buffer 丢弃、走 except。
                gen = invoke(attempt)
                for delta in gen:
                    buffer.append(delta)
                    if delta.content:
                        received_chars += len(delta.content)
                # 流正常结束（生成器耗尽且无异常）= 本次 attempt 成功。
                elapsed = time.time() - attempt_start
                self._log("model-ok",
                          f"attempt {attempt} ok ({elapsed:.1f}s, {received_chars} chars) call={call_id}")
                # atomic：一次性把本次完整内容 yield 给上层
                return ("__stream__", buffer)
            except Exception as exc:  # noqa: BLE001 —— 任何异常都分类，决定重试与否
                last_exc = exc
                last_verdict = retry_policy.classify_error(exc)
                # 日志只记分类结论 + 异常类名 + 状态码，不记异常文本（防泄漏 key/prompt）
                log_reason = (f"{type(exc).__name__}"
                              + (f" {last_verdict.status_code}" if last_verdict.status_code else ""))
                if not last_verdict.retryable:
                    # 永久错误：不重试，原样上抛（让 CodeAgent/上层处理），保留异常链
                    self._log("model-fail",
                              f"attempt {attempt} permanent ({log_reason}) call={call_id}")
                    raise
                is_last = attempt >= max_attempts
                cap_hit = (last_verdict.retry_after is not None
                           and last_verdict.retry_after > self.schedule.retry_after_cap)
                if cap_hit:
                    # 服务端要求等待超过 cap：fast-retry 不干等。先打 escalate 日志
                    # （避免先打"retry in 15s"再退出，造成误导），再向上抛。
                    self._log("model-escalate",
                              f"attempt {attempt} {log_reason}; retry-after "
                              f"{last_verdict.retry_after:.0f}s > cap "
                              f"{self.schedule.retry_after_cap}s; escalate call={call_id}")
                    break
                if is_last:
                    self._log("model-fail",
                              f"attempt {attempt}/{max_attempts} {log_reason}; "
                              f"no attempts left call={call_id}")
                    break
                delay = self.schedule.delay_for(attempt, last_verdict.retry_after)
                partial = f", partial {received_chars} chars discarded" if kind == "generate_stream" else ""
                self._log(
                    "model-retry",
                    f"attempt {attempt}/{max_attempts} {log_reason}{partial}; "
                    f"retry {attempt + 1} in {delay:.1f}s call={call_id}")
                retry_policy.sleep(delay)

        # 所有 attempt 用尽 / cap-hit 主动 break
        total = time.time() - start
        err = TransientModelUnavailable(
            f"LLM call failed after {attempts_made} attempt(s): "
            f"{last_verdict.reason if last_verdict else 'unknown'}",
            provider=self.provider,
            model=str(getattr(self.base, "model_id", "?")),
            attempts=attempts_made,
            last_error_type=type(last_exc).__name__ if last_exc else "unknown",
            last_status_code=last_verdict.status_code if last_verdict else None,
            elapsed_seconds=total,
        )
        if last_exc is not None:
            raise err from last_exc   # 保留异常链，便于上层诊断
        raise err

    # ── Model 接口实现 ─────────────────────────────────────────────────────
    def generate(
        self,
        messages: list[ChatMessage | dict],
        stop_sequences: list[str] | None = None,
        response_format: dict | None = None,
        tools_to_call_from: list | None = None,
        **kwargs,
    ) -> ChatMessage:
        def invoke(_attempt: int) -> ChatMessage:
            return self.base.generate(
                messages,
                stop_sequences=stop_sequences,
                response_format=response_format,
                tools_to_call_from=tools_to_call_from,
                **kwargs,
            )
        return self._retry_call("generate", invoke, messages)

    def generate_stream(
        self,
        messages: list[ChatMessage | dict],
        stop_sequences: list[str] | None = None,
        response_format: dict | None = None,
        tools_to_call_from: list | None = None,
        **kwargs,
    ) -> Generator[ChatMessageStreamDelta, None, None]:
        # 每次 attempt 都新建一个生成器（Python 生成器一次性的，重试必须重建）。
        def make_gen(_attempt: int):
            return self.base.generate_stream(
                messages,
                stop_sequences=stop_sequences,
                response_format=response_format,
                tools_to_call_from=tools_to_call_from,
                **kwargs,
            )

        result = self._retry_call("generate_stream", make_gen, messages)
        # _retry_call 返回 ("__stream__", buffer)；这里 atomic 地重新 yield 出去。
        _marker, buffer = result
        for delta in buffer:
            yield delta
