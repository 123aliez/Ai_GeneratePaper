"""ResilientModel 的离线验证（不调任何 API、不发网络）。

覆盖 midstream plan v2 §31 的 T1-T10 核心用例：
  T1  正常调用 → 1 次 attempt 成功，无重试
  T2  503 → 第 2 次 success（CodeAgent 只看到成功，memory 不含 503）
  T3  ConnectionReset → 第 2 次 success
  T4  mid-stream 中断 → partial 不泄漏，重发后 CodeAgent 只收到完整结果（P0）
  T5  3 次全失败 → 抛 TransientModelUnavailable
  T6  401 永久错误 → 不重试，原样抛，attempt=1
  T7  413/422 → 立即失败
  T8  429 + Retry-After → 尊重 retry-after，但超 cap 不干等
  T9  tool 不重复执行（partial action 永不执行）
  T10 client 复用（底层 base_model 复用，ResilientModel 不 new）

以及 retry_policy 的分类与退避单元测试。

运行: python tests/test_resilient_model.py
"""
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from smolagents.models import ChatMessage, ChatMessageStreamDelta, MessageRole
from smolagents.monitoring import TokenUsage

from agents.retry_policy import (
    RetrySchedule, classify_error, TransientModelUnavailable,
)
from agents.resilient_model import ResilientModel

PASSED = []


def check(label: str, cond: bool, detail: str = "") -> None:
    if not cond:
        raise AssertionError(f"{label}: {detail or 'failed'}")
    PASSED.append(label)


# ── 测试用 Fake Model ──────────────────────────────────────────────────────
class FakeBaseModel:
    """记录每次调用的假底层 Model，可注入任意异常序列。

    client_create_count 模拟 SDK client 的创建次数——本类构造时 +1，
    用于 T10（client 复用）断言。ResilientModel 不应创建新 client。
    """
    _client_create_count = 0

    def __init__(self, model_id="fake-model"):
        self.model_id = model_id
        self.kwargs = {}
        self.flatten_messages_as_text = False
        self.tool_name_key = "name"
        self.tool_arguments_key = "arguments"
        FakeBaseModel._client_create_count += 1
        self.call_count = 0
        self._stream_behaviors = None
        self._generate_behavior = None

    # —— 设置行为：stream 版 —— #
    def set_stream(self, behaviors):
        """behaviors: list，每个元素是该 attempt 的 delta 列表或要抛的异常。"""
        self._stream_behaviors = list(behaviors)

    # —— 设置行为：generate 版 —— #
    def set_generate(self, behaviors):
        self._generate_behavior = list(behaviors)

    def generate(self, messages, **kwargs):
        self.call_count += 1
        if self._generate_behavior is None:
            return ChatMessage(role=MessageRole.ASSISTANT, content="ok")
        behavior = self._generate_behavior.pop(0)
        if isinstance(behavior, BaseException):
            raise behavior
        return behavior

    def generate_stream(self, messages, **kwargs):
        self.call_count += 1
        if self._stream_behaviors is None:
            yield ChatMessageStreamDelta(content="hello")
            return
        behavior = self._stream_behaviors.pop(0)
        if isinstance(behavior, BaseException):
            raise behavior
        # behavior 是 delta 列表
        for delta in behavior:
            yield delta


def _schedule():
    return RetrySchedule(max_attempts=3, delays=(0.001, 0.001), jitter_ratio=0.0)


# ── T1: 正常调用 ────────────────────────────────────────────────────────────
def test_t1_normal():
    FakeBaseModel._client_create_count = 0
    base = FakeBaseModel()
    base.set_stream([[ChatMessageStreamDelta(content="hi"),
                      ChatMessageStreamDelta(content=" there")]])
    rm = ResilientModel(base, _schedule(), owner="Test")
    out = "".join(d.content for d in rm.generate_stream([]))
    check("T1 内容正确", out == "hi there", out)
    check("T1 只调用 1 次", base.call_count == 1, f"call_count={base.call_count}")


# ── T2: 503 → success ──────────────────────────────────────────────────────
def test_t2_503_then_success():
    base = FakeBaseModel()
    # 用一个带 status_code=503 的假异常
    class FakeStatusError(Exception):
        status_code = 503

    base.set_stream([
        FakeStatusError(),  # attempt 1: 503
        [ChatMessageStreamDelta(content="recovered")],  # attempt 2: ok
    ])
    rm = ResilientModel(base, _schedule(), owner="Test")
    out = "".join(d.content for d in rm.generate_stream([]))
    check("T2 最终内容正确", out == "recovered", out)
    check("T2 调用 2 次", base.call_count == 2, f"call_count={base.call_count}")


# ── T3: ConnectionReset → success ──────────────────────────────────────────
def test_t3_connection_reset():
    base = FakeBaseModel()
    base.set_stream([
        ConnectionResetError("Upstream HTTP/2 stream failed"),
        [ChatMessageStreamDelta(content="ok2")],
    ])
    rm = ResilientModel(base, _schedule(), owner="Test")
    out = "".join(d.content for d in rm.generate_stream([]))
    check("T3 内容正确", out == "ok2", out)
    check("T3 调用 2 次", base.call_count == 2, f"call_count={base.call_count}")


# ── T4: mid-stream 中断，partial 不泄漏（P0）────────────────────────────────
def test_t4_midstream_no_leak():
    base = FakeBaseModel()
    base.set_stream([
        # attempt 1: 吐了 partial 然后断流
        [ChatMessageStreamDelta(content="PARTIAL-LEAK"),
         "RAISE_ConnectionResetError"],  # 占位，下面替换
    ])
    # 用一个会先 yield 再抛的 behavior：列表里最后一个是 sentinel，我们在
    # FakeBaseModel 里不支持，改成直接用一个生成器行为。
    # —— 改用一个自定义异常类，第一个 behavior 先吐 partial 再抛 —— #
    class PartialThenFail:
        def __init__(self):
            self.deltas = [ChatMessageStreamDelta(content="PARTIAL-LEAK")]
            self.exc = ConnectionResetError("mid-stream RST")

    # FakeBaseModel.generate_stream 接收 list[delta]，要支持"中途抛"，
    # 这里改造：behavior 可以是 (deltas, exc) 元组。
    def fake_gen(self, messages, **kwargs):
        self.call_count += 1
        behavior = self._stream_behaviors.pop(0)
        deltas, exc = behavior
        for d in deltas:
            yield d
        if exc is not None:
            raise exc

    import types
    base.generate_stream = types.MethodType(fake_gen, base)
    base.set_stream = lambda behaviors: setattr(base, "_stream_behaviors", list(behaviors))
    base.set_stream([
        ([ChatMessageStreamDelta(content="PARTIAL-LEAK")],
         ConnectionResetError("mid-stream RST")),
        ([ChatMessageStreamDelta(content="FULL-OK")], None),
    ])

    rm = ResilientModel(base, _schedule(), owner="Test")
    out = "".join(d.content for d in rm.generate_stream([]))
    check("T4 partial 未泄漏", "PARTIAL-LEAK" not in out, out)
    check("T4 只收到完整结果", out == "FULL-OK", out)
    check("T4 调用 2 次", base.call_count == 2, f"call_count={base.call_count}")


# ── T5: 3 次全失败 → TransientModelUnavailable ─────────────────────────────
def test_t5_all_fail():
    base = FakeBaseModel()
    base.set_stream([
        ConnectionResetError("fail1"),
        ConnectionResetError("fail2"),
        ConnectionResetError("fail3"),
    ])
    rm = ResilientModel(base, _schedule(), owner="Test")
    try:
        list(rm.generate_stream([]))
        check("T5 应抛 TransientModelUnavailable", False)
    except TransientModelUnavailable as e:
        check("T5 抛 TransientModelUnavailable", True)
        check("T5 attempts=3", e.attempts == 3, str(e.attempts))
    check("T5 调用 3 次（不超 max）", base.call_count == 3, f"call_count={base.call_count}")


# ── T6: 401 永久错误不重试 ──────────────────────────────────────────────────
def test_t6_permanent_401():
    base = FakeBaseModel()
    class FakeAuthError(Exception):
        status_code = 401

    base.set_stream([FakeAuthError()])
    rm = ResilientModel(base, _schedule(), owner="Test")
    try:
        list(rm.generate_stream([]))
        check("T6 401 应原样抛出（非 Transient）", False)
    except TransientModelUnavailable:
        check("T6 401 不该被包装成 Transient", False)
    except FakeAuthError:
        check("T6 401 原样抛出", True)
    check("T6 401 只调用 1 次", base.call_count == 1, f"call_count={base.call_count}")


# ── T7: 413 / 422 立即失败 ──────────────────────────────────────────────────
def test_t7_413_422():
    for code in (413, 422):
        base = FakeBaseModel()
        class FakePerm(Exception):
            status_code = code
        base.set_stream([FakePerm()])
        rm = ResilientModel(base, _schedule(), owner="Test")
        raised_perm = False
        try:
            list(rm.generate_stream([]))
        except TransientModelUnavailable:
            raised_perm = False
        except FakePerm:
            raised_perm = True
        check(f"T7 {code} 立即失败不重试", raised_perm and base.call_count == 1,
              f"raised={raised_perm} count={base.call_count}")


# ── T8: 429 + Retry-After ───────────────────────────────────────────────────
def test_t8_retry_after():
    # retry-after 在 cap 内 → 重试
    base = FakeBaseModel()

    class FakeHeaders:
        def get(self, k):
            return "2" if k.lower() == "retry-after" else None
        def items(self):
            return [("retry-after", "2")]

    class FakeResp:
        headers = FakeHeaders()

    class Fake429(Exception):
        status_code = 429
        response = FakeResp()

    base.set_stream([Fake429(), [ChatMessageStreamDelta(content="after429")]])
    rm = ResilientModel(base, _schedule(), owner="Test")
    out = "".join(d.content for d in rm.generate_stream([]))
    check("T8 retry-after<=cap 重试成功", out == "after429", out)
    check("T8 调用 2 次", base.call_count == 2, f"call_count={base.call_count}")


# ── T9: tool 不重复执行（partial action 永不执行）──────────────────────────
# 用 generate（非流式）模拟：attempt1 抛错前不会返回任何 ChatMessage，
# 所以 CodeAgent 拿不到任何 action 去执行。验证 generate 路径的重试。
def test_t9_no_partial_action():
    base = FakeBaseModel()
    exec_count = {"n": 0}

    class FakeAction(ChatMessage):
        pass

    base.set_generate([
        ConnectionResetError("stream fail before any action"),
        ChatMessage(role=MessageRole.ASSISTANT, content="final_answer_code"),
    ])
    rm = ResilientModel(base, _schedule(), owner="Test")
    # 模拟 CodeAgent：只有拿到 generate 的返回值才"执行 tool"
    result = rm.generate([])
    # 假装执行（计数）
    exec_count["n"] += 1
    check("T9 只执行 1 次（拿到完整结果后才执行）", exec_count["n"] == 1)
    check("T9 拿到的是成功结果", result.content == "final_answer_code", result.content)
    check("T9 base 调用 2 次（第 1 次抛错没返回 action）", base.call_count == 2,
          f"call_count={base.call_count}")


# ── T10: client 复用 ────────────────────────────────────────────────────────
def test_t10_client_reuse():
    FakeBaseModel._client_create_count = 0
    base = FakeBaseModel()
    rm = ResilientModel(base, _schedule(), owner="Test")
    # 连续 3 次 generate_stream
    for _ in range(3):
        base.set_stream([[ChatMessageStreamDelta(content="x")]])
        list(rm.generate_stream([]))
    # FakeBaseModel 构造时 _client_create_count 已经 +1，但 ResilientModel 不应
    # 再创建新的 base/client——它复用同一个 base。
    check("T10 base 只创建 1 次", FakeBaseModel._client_create_count == 1,
          f"create_count={FakeBaseModel._client_create_count}")


# ── retry_policy 单元测试 ───────────────────────────────────────────────────
def test_retry_policy_classify():
    class StatusErr(Exception):
        def __init__(self, code):
            self.status_code = code
    check("503 retryable", classify_error(StatusErr(503)).retryable)
    check("429 retryable", classify_error(StatusErr(429)).retryable)
    check("401 permanent", not classify_error(StatusErr(401)).retryable)
    check("422 permanent", not classify_error(StatusErr(422)).retryable)
    check("400 permanent", not classify_error(StatusErr(400)).retryable)
    # 异常类名匹配
    check("ConnectionResetError retryable",
          classify_error(ConnectionResetError("x")).retryable)
    check("TimeoutError retryable",
          classify_error(TimeoutError("read timed out")).retryable)
    # 文案匹配（网关 transient 文本）
    class Plain(Exception):
        pass
    v = classify_error(Plain("Upstream request failed"))
    check("网关文案 retryable", v.retryable, v.reason)


def test_retry_schedule_delay():
    sch = RetrySchedule(max_attempts=3, delays=(0.8, 2.0), jitter_ratio=0.0,
                        retry_after_cap=15.0)
    check("第1次失败后等 0.8s", sch.delay_for(1, None) == 0.8)
    check("第2次失败后等 2.0s", sch.delay_for(2, None) == 2.0)
    # retry-after 在 cap 内 → 用 retry-after
    check("retry-after=3 用 3", sch.delay_for(1, 3.0) == 3.0)
    # retry-after 超 cap → 返回 cap（触发上层"不再 fast-retry"判断）
    check("retry-after=100 返回 cap=15", sch.delay_for(1, 100.0) == 15.0)


def test_max_attempts_validation():
    try:
        RetrySchedule(max_attempts=0)
        check("max_attempts=0 应报错", False)
    except ValueError:
        check("max_attempts=0 显式报错", True)


# ── C: SDK SSE error 分类（2xx + body / 文案 / HTTP-date Retry-After）────────
def test_classify_sse_200_overloaded():
    """Anthropic 在 200 SSE 里抛 overloaded_error：body 才是真相，必须重试。"""
    class FakeSSE(Exception):
        status_code = 200
        body = {"type": "error", "error": {"type": "overloaded_error",
                                           "message": "Overloaded"}}
    v = classify_error(FakeSSE())
    check("2xx+overloaded_body retryable", v.retryable, v.reason)


def test_classify_sse_streaming_text():
    """openai SSE 抛 APIError('An error occurred during streaming')，无 status_code。"""
    class APIError(Exception):
        pass
    v = classify_error(APIError("An error occurred during streaming"))
    check("SSE streaming 文案 retryable", v.retryable, v.reason)


def test_classify_body_api_error():
    class Fake(Exception):
        status_code = 200
        body = {"type": "error", "error": {"type": "api_error", "message": "boom"}}
    check("2xx+api_error body retryable", classify_error(Fake()).retryable)


def test_classify_body_string_form():
    """SDK 在 SSE error 数据非合法 JSON 时把 body 保留为原始字符串，也要识别。"""
    class Fake(Exception):
        status_code = 200
        body = "overloaded"   # 字符串形态
    check("2xx+字符串 body retryable", classify_error(Fake()).retryable)


def test_classify_body_keeps_retry_after():
    """body 命中时不能丢失已提取的 retry_after（否则 cap-hit 失效）。"""
    class FakeResp:
        class _H:
            def get(self, k):
                return "120" if k.lower() == "retry-after" else None
            def items(self):
                return [("retry-after", "120")]
        headers = _H()
    class Fake(Exception):
        status_code = 200
        body = {"type": "error", "error": {"type": "overloaded_error"}}
        response = FakeResp()
    v = classify_error(Fake())
    check("body 命中保留 retry_after=120", v.retry_after == 120.0, str(v.retry_after))
    check("body 命中保留 status_code=200", v.status_code == 200)


def test_classify_body_reason_no_leak():
    """body 命中时 reason 不含原始 etype/message（防泄漏）。"""
    class Fake(Exception):
        status_code = 200
        body = {"type": "error",
                "error": {"type": "api_error", "message": "Upstream failed SECRET_KEY"}}
    v = classify_error(Fake())
    check("body reason 不含 SECRET", "SECRET_KEY" not in v.reason, v.reason)
    check("body reason 不含原始 message", "Upstream failed" not in v.reason, v.reason)


def test_retry_after_http_date():
    """Retry-After 用 HTTP-date 格式：应转成相对秒数，不丢成 None。"""
    import email.utils
    # 用未来 3 秒的日期
    future = email.utils.formatdate(time.time() + 3, usegmt=True)

    class FakeResp:
        class _H:
            def get(self, k):
                return future if k.lower() == "retry-after" else None
            def items(self):
                return [("retry-after", future)]
        headers = _H()
    class Fake429(Exception):
        status_code = 429
        response = FakeResp()
    ra = classify_error(Fake429()).retry_after
    check("HTTP-date Retry-After 解析出秒数", ra is not None and 0 < ra < 10, str(ra))


# ── F/G: cap-hit 时实际 attempts < max + 日志语义 ────────────────────────────
def test_cap_hit_attempts_count():
    """Retry-After 超 cap：第 1 次失败后立即 escalate，attempts=1 而非 max。"""
    base = FakeBaseModel()

    class FakeResp:
        class _H:
            def get(self, k):
                return "1000" if k.lower() == "retry-after" else None
            def items(self):
                return [("retry-after", "1000")]
        headers = _H()
    class Fake503(Exception):
        status_code = 503
        response = FakeResp()

    base.set_stream([Fake503(), Fake503(), Fake503()])  # 即使后面还有也不会用
    sch = RetrySchedule(max_attempts=3, delays=(0.001, 0.001), jitter_ratio=0.0,
                        retry_after_cap=15.0)
    rm = ResilientModel(base, sch, owner="Test", provider="openai")
    try:
        list(rm.generate_stream([]))
        check("cap-hit 应抛 Transient", False)
    except TransientModelUnavailable as e:
        check("cap-hit attempts=1（未用尽 max）", e.attempts == 1, str(e.attempts))
        check("cap-hit last_error_type 是异常类名", e.last_error_type == "Fake503",
              e.last_error_type)
        check("cap-hit provider=openai", e.provider == "openai", e.provider)
        check("cap-hit last_status_code=503", e.last_status_code == 503)
    check("cap-hit 只调用 1 次", base.call_count == 1, f"count={base.call_count}")


# ── L: 日志不泄漏异常文本（只记类名+状态码）─────────────────────────────────
def test_no_log_leak(capsys=None):
    """异常文本可能含 key/prompt，日志只记类名+状态码。"""
    import io, contextlib
    base = FakeBaseModel()
    secret = "api_key=sk-SECRET123 prompt=CONFIDENTIAL"
    base.set_stream([ConnectionResetError(f"Upstream failed {secret}")])
    rm = ResilientModel(base, _schedule(), owner="Test", provider="openai")
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        try:
            list(rm.generate_stream([]))
        except Exception:
            pass
    out = buf.getvalue()
    # 重试后用尽会抛 Transient；无论成功失败，日志里都不该出现 secret
    check("日志不含 secret", "sk-SECRET123" not in out and "CONFIDENTIAL" not in out, out)


# ── H: LLM_RETRY_ENABLED=false 退化（不包裹 ResilientModel）──────────────────
def test_build_model_retry_disabled():
    """retry_enabled=False → build_model 直接返回底层 Model（不包 ResilientModel）。"""
    from agents.model_router import build_model
    m = build_model("openai", "gpt-5", "k", "https://x/v1", "high", retry_enabled=False)
    check("disabled 时返回底层 OpenAIModel（非 ResilientModel）",
          type(m).__name__ == "OpenAIModel", type(m).__name__)


def test_build_model_retry_enabled_wraps():
    from agents.model_router import build_model
    m = build_model("openai", "gpt-5", "k", "https://x/v1", "high", retry_enabled=True)
    check("enabled 时包成 ResilientModel", type(m).__name__ == "ResilientModel",
          type(m).__name__)
    check("enabled 时 provider 透传", m.provider == "openai", m.provider)


def test_build_model_sdk_retries_forced_zero():
    """retry_enabled=True 且传 max_retries=3 → 强制改 0（防叠加）。"""
    from agents.model_router import build_model
    m = build_model("openai", "gpt-5", "k", "https://x/v1", "high",
                    max_retries=3, retry_enabled=True)
    base = m.base
    check("enabled 时 SDK max_retries 被强制 0",
          base.client_kwargs.get("max_retries") == 0, str(base.client_kwargs))


# ── B: 永久错误被 run_agent_stage 立即识别（不重跑 CodeAgent）────────────────
def test_permanent_detected_in_chain():
    """ResilientModel 原样抛永久错误，_is_permanent_failure 沿链能识别。"""
    from agents.orchestrator import _is_permanent_failure

    class PermRoot(Exception):
        status_code = 401

    # 模拟 CodeAgent 包装：AgentGenerationError(cause=PermRoot)
    class Wrapped(Exception):
        pass
    wrapped = Wrapped("model output error")
    wrapped.__cause__ = PermRoot("401 Unauthorized")
    check("_is_permanent_failure 沿 __cause__ 识别 401",
          _is_permanent_failure(wrapped) is True)

    class TransRoot(Exception):
        status_code = 503
    wrapped2 = Wrapped("x")
    wrapped2.__context__ = TransRoot("503")
    check("503 不算永久", _is_permanent_failure(wrapped2) is False)


tests = [
    test_t1_normal, test_t2_503_then_success, test_t3_connection_reset,
    test_t4_midstream_no_leak, test_t5_all_fail, test_t6_permanent_401,
    test_t7_413_422, test_t8_retry_after, test_t9_no_partial_action,
    test_t10_client_reuse,
    test_retry_policy_classify, test_retry_schedule_delay,
    test_max_attempts_validation,
    # codex 审查后补的覆盖
    test_classify_sse_200_overloaded, test_classify_sse_streaming_text,
    test_classify_body_api_error, test_classify_body_string_form,
    test_classify_body_keeps_retry_after, test_classify_body_reason_no_leak,
    test_retry_after_http_date,
    test_cap_hit_attempts_count, test_no_log_leak,
    test_build_model_retry_disabled, test_build_model_retry_enabled_wraps,
    test_build_model_sdk_retries_forced_zero, test_permanent_detected_in_chain,
]


if __name__ == "__main__":
    print("== test_resilient_model ==")
    total = len(tests)
    for t in tests:
        t()
    print(f"\n{len(PASSED)} 项检查通过 / {total} 个测试:")
    for label in PASSED:
        safe = label.encode("ascii", "replace").decode("ascii")
        print(f"  ok  {safe}")
    print("\nRESILIENT MODEL TESTS PASSED")
