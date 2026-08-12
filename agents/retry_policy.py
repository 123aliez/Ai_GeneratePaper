"""LLM 调用的瞬时错误分类与快速退避策略（ResilientModel 专用）。

定位
----
网关对长流式响应不稳：reasoning=max 时单次响应数分钟，期间网关任意一刻
``HTTP/2 RST`` / ``Upstream request failed`` / ``service temporarily unavailable``
都会把流式中途掐断。openai / anthropic SDK 的 ``max_retries`` 只覆盖**连接级**
失败，**不覆盖"流式已开始但中途断开"**——那种半截流被 smolagents 包成
``AgentGenerationError``，冒泡到 ``run_agent_stage`` 触发整个 ``CodeAgent.run()``
重跑，每次重跑几分钟，串行几十次请求后表现为"分钟级空窗"。

正确做法是把 retry 边界下沉到**单次 logical model call**：在 Model 层用 atomic
buffer 收完整个流，中途断了就丢弃 partial、快速重发**相同请求**。ResilientModel
是唯一的瞬时重试 owner，SDK / smolagents / run_agent_stage 三层都不再做网络重试。

本模块只负责"判断这次错误该不该重试"和"重试前睡多久"，不碰 Model/流式逻辑。
"""
from __future__ import annotations

import random
import time
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from typing import Any


# ── 唯一向上冒泡的异常 ──────────────────────────────────────────────────
class TransientModelUnavailable(Exception):
    """ResilientModel 把单次 logical call 重试到上限仍未成功时抛出。

    与普通 ``Exception`` 区分：``run_agent_stage`` 见到它才做 stage 级兜底重跑，
    其它异常（解析错、永久错）不应当触发整个 CodeAgent 重启。
    """

    def __init__(self, message: str, *, provider: str, model: str, attempts: int,
                 last_error_type: str, last_status_code: int | None = None,
                 elapsed_seconds: float = 0.0):
        super().__init__(message)
        self.provider = provider
        self.model = model
        self.attempts = attempts
        self.last_error_type = last_error_type
        self.last_status_code = last_status_code
        self.elapsed_seconds = elapsed_seconds

    def __str__(self) -> str:  # 给终端一行可读的总结，不塞 traceback
        sc = f" status={self.last_status_code}" if self.last_status_code is not None else ""
        return (f"{type(self).__name__}({self.provider}/{self.model} "
                f"after {self.attempts} attempts{sc}, last={self.last_error_type}, "
                f"{self.elapsed_seconds:.1f}s): {self.args[0]}")


# ── 错误分类 ────────────────────────────────────────────────────────────
# 该重试的 HTTP 状态码（连接已建立、服务端暂态）。Anthropic 529（overloaded）也算。
RETRYABLE_STATUS = frozenset({408, 409, 429, 500, 502, 503, 504, 529})

# 永久错误：重复请求不会自行恢复，重试只浪费时间和 token。
PERMANENT_STATUS = frozenset({400, 401, 402, 403, 404, 413, 422})

# 网关文案兜底：自建网关把上游故障压平成一句英文，没有专属异常类型，
# 只能按字符串认。覆盖实测文案 + OpenAI/Anthropic SDK 在流式中途抛的常见文案。
_TRANSIENT_TEXT_PATTERNS = (
    "upstream service temporarily unavailable",
    "upstream request failed",
    "upstream http/2 stream failed",
    "stream failed",
    "remote protocol error",
    "connection reset",
    "peer closed the connection",
    "incomplete chunked read",
    "unexpected eof",
    "server disconnected",
    "timed out",
    "connection error",
    "connection closed",
    "the server had an error while processing your request",
    "an error occurred during streaming",
    "http/2 rst_stream",
    "rst_stream",
    "overloaded",
)

# 连接级 / 流式传输级异常的"类名片段"——SDK 各版本类名略有差异，按片段匹配更稳。
# 覆盖 httpx 原生异常 + openai/anthropic SDK 包装类。
_TRANSIENT_EXC_NAME_PATTERNS = (
    "connectionerror", "connectionreseterror", "connecttimeout", "readtimeout",
    "writetimeout", "pooltimeout", "readerror", "writeerror",
    "remoteprotocolerror", "protocolexception", "localprotocolerror",
    "apiconnectionerror", "apitimeouterror", "internalservererror",
    "ratelimiterror", "conflicterror", "badgatewayerror", "overloadederror",
    "apierror",  # openai APIError（含 SSE streaming error）按需重试
)


@dataclass(frozen=True)
class ErrorVerdict:
    """对一次异常的分类结果。"""
    retryable: bool
    status_code: int | None
    retry_after: float | None      # 服务端建议的等待秒数（来自 Retry-After / Retry-After-ms）
    reason: str                    # 给日志/诊断用的一句人话


def _extract_status_code(exc: BaseException) -> int | None:
    """从 SDK 异常里挖 HTTP 状态码（openai/anthropic 都把 status_code 放在属性上）。"""
    for attr in ("status_code", "statusCode"):
        val = getattr(exc, attr, None)
        if isinstance(val, int):
            return val
    response = getattr(exc, "response", None)
    if response is not None:
        val = getattr(response, "status_code", None)
        if isinstance(val, int):
            return val
    return None


def _extract_retry_after(exc: BaseException) -> float | None:
    """读 Retry-After / Retry-After-ms 响应头（秒）。无法解析或没有则 None。

    支持：``Retry-After-ms``（毫秒）、``Retry-After``（整数/小数秒）、以及 HTTP-date
    格式（``Tue, 11 Aug 2026 13:00:00 GMT``）——后者按相对当前时间的秒数返回。
    """
    response = getattr(exc, "response", None)
    if response is None:
        return None
    headers = getattr(response, "headers", None)
    if not headers:
        return None
    # 优先毫秒精度（部分网关用 Retry-After-ms）
    for key in ("retry-after-ms", "retry_after_ms"):
        raw = _get_header_ci(headers, key)
        if raw is not None:
            try:
                return max(0.0, float(raw) / 1000.0)
            except (TypeError, ValueError):
                pass
    raw = _get_header_ci(headers, "retry-after")
    if raw is None:
        return None
    # 先按数字秒解析
    try:
        return max(0.0, float(raw))
    except (TypeError, ValueError):
        pass
    # 再按 HTTP-date 解析（RFC 7231）
    try:
        dt = parsedate_to_datetime(raw)
        if dt is not None:
            delta = dt.timestamp() - time.time()
            return max(0.0, float(delta))
    except (TypeError, ValueError, OverflowError, OSError):
        pass
    return None  # 无法解析 → 退到默认 backoff


def _get_header_ci(headers: Any, name: str) -> str | None:
    """大小写不敏感地从 Mapping/httpx.Headers 取一个 header 值。"""
    try:
        # httpx.Headers / requests.CaseInsensitiveDict 都支持小写取值
        if hasattr(headers, "get"):
            val = headers.get(name)
            if val is not None:
                return str(val)
        for k, v in headers.items():
            if str(k).lower() == name.lower():
                return str(v)
    except Exception:
        pass
    return None


def _classify_body(exc: BaseException, status: int | None,
                   retry_after: float | None) -> ErrorVerdict | None:
    """检查 Anthropic/OpenAI SDK 异常的 ``body`` 字段里携带的结构化错误。

    Anthropic 在 HTTP 200 的 SSE 流里用 ``error`` 事件抛
    ``APIStatusError(status_code=200, body={...})``；这种 status_code=200 但 body
    表明服务端暂态，必须按文案/body 重试。body 也可能是字符串（SSE error 数据不是
    合法 JSON 时 SDK 会保留原始字符串）。返回 None 表示 body 里没找到可判定的暂态信号。

    保留已提取的 ``status`` 与 ``retry_after``——HTTP 200 SSE 可能同时带
    ``Retry-After: 120``，丢失 retry_after 会让 cap-hit 失效。
    """
    body = getattr(exc, "body", None)
    # body 可能是 dict、str（SDK 兜底）、或其它类型；统一抽出"可扫描的文本"
    etype = ""
    message = ""
    if isinstance(body, dict):
        err = body.get("error") if isinstance(body.get("error"), dict) else body
        if isinstance(err, dict):
            etype = str(err.get("type", err.get("type_"))).lower()
            message = str(err.get("message", "")).lower()
    elif isinstance(body, str):
        message = body.lower()

    if not (etype or message):
        return None
    # overloaded_error / api_error / server_error / rate_limit_error 都是暂态。
    # 用固定模式标识做 reason，不把原始 etype/message 写进 verdict（防日志泄漏）。
    transient_types = ("overloaded", "overloaded_error", "api_error", "server_error",
                       "rate_limit_error", "timeout")
    if any(t in etype for t in transient_types):
        return ErrorVerdict(True, status, retry_after, "transient body type")
    if any(p in message for p in _TRANSIENT_TEXT_PATTERNS):
        return ErrorVerdict(True, status, retry_after, "transient body text")
    return None


def classify_error(exc: BaseException) -> ErrorVerdict:
    """判断一次异常是否值得快速重试，并带出状态码与 Retry-After。

    判定优先级：
      1. HTTP 状态码在 ``RETRYABLE_STATUS`` → 重试
      2. HTTP 状态码在 ``PERMANENT_STATUS`` → 不重试（401/413/422 这类）
      3. 2xx（含 200）+ 带 body 的 SSE 流错误 → 查 body（Anthropic 重载错误常是 200）
      4. 异常类名命中连接/超时/限流类 → 重试
      5. 异常文案命中网关 transient 文案 → 重试
      6. 其它 → 不重试（未知异常交给上层，避免对不可控错误无限重试）

    故意保守：宁可少重试（让 stage 级兜底接手），也不要对不可恢复的错误烧 token。
    但 2xx+body 这一路必须查——否则 Anthropic SSE 中断会被当成"200 成功"漏掉。
    reason 字段只用固定模式标识，绝不写入异常原文（防 key/prompt 泄漏到日志）。
    """
    status = _extract_status_code(exc)
    retry_after = _extract_retry_after(exc)

    if status is not None:
        if status in RETRYABLE_STATUS:
            return ErrorVerdict(True, status, retry_after,
                                f"retryable HTTP {status}")
        if status in PERMANENT_STATUS:
            return ErrorVerdict(False, status, retry_after,
                                f"permanent HTTP {status}")
        # 2xx（含 200）：Anthropic 在 200 SSE 里抛 overloaded/api_error，body 才是真相
        if 200 <= status < 300:
            body_verdict = _classify_body(exc, status, retry_after)
            if body_verdict is not None:
                return body_verdict
            # 2xx 但 body 无暂态信号——继续往下查类名/文案（body 是非 JSON 字符串时
            # _classify_body 可能返回 None，但文案仍可能命中）。
        # 未知状态码：3xx、其它 4xx/5xx 保守不重试
        return ErrorVerdict(False, status, retry_after,
                            f"unclassified HTTP {status} (not retried)")

    exc_name = type(exc).__name__.lower()
    if any(p in exc_name for p in _TRANSIENT_EXC_NAME_PATTERNS):
        return ErrorVerdict(True, None, retry_after,
                            f"transient exception type {type(exc).__name__}")

    # 再查 body（无 status_code 但带 body 的 SDK 异常）
    body_verdict = _classify_body(exc, status, retry_after)
    if body_verdict is not None:
        return body_verdict

    msg = str(exc).lower()
    if any(p in msg for p in _TRANSIENT_TEXT_PATTERNS):
        return ErrorVerdict(True, None, retry_after,
                            "transient error text matched")

    return ErrorVerdict(False, None, retry_after,
                        f"unclassified {type(exc).__name__} (not retried)")


# ── 退避 ────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class RetrySchedule:
    """快速重试计划：短退避 + jitter，目标是"断了立刻重连"而不是长等待。

    长退避会放大成用户感知的分钟级空窗，且网关抖动通常几百毫秒就恢复。
    ``retry_after_cap`` 给服务端 ``Retry-After`` 设上限——超过上限说明服务端
    真的不可用，应当交给 stage 级兜底，而不是占用 fast-retry 名额干等。
    """
    max_attempts: int = 3
    delays: tuple[float, ...] = (0.8, 2.0)        # 第 1、2 次失败后的等待（最后 1 次不睡）
    jitter_ratio: float = 0.25
    retry_after_cap: float = 15.0

    def delay_for(self, attempt_failed_count: int, retry_after: float | None) -> float:
        """计算下一次 attempt 前的睡眠秒数。

        attempt_failed_count 是已失败的次数（1 = 第 1 次刚失败，将进入第 2 次）。
        优先尊重服务端 ``Retry-After``（受 cap 限制），否则用固定档位 + jitter。
        最后一次 attempt 不在这里 sleep（调用方控制）。
        """
        if retry_after is not None:
            if retry_after <= self.retry_after_cap:
                return retry_after
            # 超过 cap：服务端明确要求长等待，不归 fast-retry 管——返回 cap 触发
            # 调用方"不再快速重试、向上抛"的判断。
            return self.retry_after_cap
        idx = min(attempt_failed_count - 1, len(self.delays) - 1)
        base = self.delays[idx] if self.delays else 0.0
        if self.jitter_ratio <= 0:
            return base
        # 对称 jitter：base ± jitter_ratio*base，钳到 >= 0
        span = base * self.jitter_ratio
        return max(0.0, base + random.uniform(-span, span))

    def __post_init__(self) -> None:
        if self.max_attempts < 1:
            raise ValueError(f"max_attempts 必须 >= 1，得到 {self.max_attempts}")
        if not 0.0 <= self.jitter_ratio <= 1.0:
            raise ValueError(f"jitter_ratio 应在 [0,1]，得到 {self.jitter_ratio}")


def sleep(delay: float) -> None:
    """可被测试 monkeypatch 的 sleep 入口。负数/None 视为 0。"""
    if delay is None or delay <= 0:
        return
    time.sleep(delay)
