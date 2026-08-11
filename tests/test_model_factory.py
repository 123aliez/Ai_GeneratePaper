"""模型调用框架（双协议 + reasoning 映射）的离线验证。

不调任何 API、不发网络。覆盖：
  1. provider 路由：openai → smolagents OpenAIModel；anthropic → AnthropicModel
  2. reasoning_level 映射：openai→reasoning_effort；anthropic→output_config.effort / thinking(off)
  3. 能力校验：传入模型不支持的档位 → 显式 ValueError（不静默降级）
  4. 6 档语义全覆盖（off/low/medium/high/xhigh/max）

运行: python tests/test_model_factory.py
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from agents.model_capabilities import (
    map_reasoning, supported_levels, ALL_LEVELS, OFF, LOW, MEDIUM, HIGH, XHIGH, MAX,
)
from agents.model_router import build_model

PASSED = []


def check(label: str, condition: bool, detail: str = "") -> None:
    if not condition:
        raise AssertionError(f"{label}: {detail or 'failed'}")
    PASSED.append(label)


# ── 1. reasoning_level 6 档齐全 ──────────────────────────────────────────────
def test_levels_complete():
    check("6 档齐全", set(ALL_LEVELS) == {OFF, LOW, MEDIUM, HIGH, XHIGH, MAX},
          f"got {ALL_LEVELS}")


# ── 2. OpenAI 映射：6 档 → reasoning_effort（off→none）────────────────────────
def test_openai_mapping():
    cases = {OFF: "none", LOW: "low", MEDIUM: "medium", HIGH: "high", XHIGH: "xhigh", MAX: "max"}
    for level, expected in cases.items():
        out = map_reasoning("openai", "gpt-5", level)
        check(f"openai {level} → reasoning_effort={expected}",
              out == {"reasoning_effort": expected}, f"got {out}")


# ── 3. Anthropic 映射：claude-opus-4-6 支持 low/medium/high/max；off→报错 ──────
def test_anthropic_mapping():
    # claude-opus-4-6 支持 max 但不支持 xhigh（xhigh 从 4.7 起）
    for level, expected in {LOW: "low", MEDIUM: "medium", HIGH: "high",
                            MAX: "max"}.items():
        out = map_reasoning("anthropic", "claude-opus-4-6", level)
        check(f"anthropic opus-4-6 {level} → output_config.effort={expected}",
              out == {"output_config": {"effort": expected}}, f"got {out}")
    # opus-4-7 支持 xhigh
    check("anthropic opus-4-7 xhigh → output_config.effort=xhigh",
          map_reasoning("anthropic", "claude-opus-4-7", XHIGH)
          == {"output_config": {"effort": "xhigh"}})
    # off：Claude effort 模型不支持，应报错
    try:
        map_reasoning("anthropic", "claude-opus-4-6", OFF)
        check("anthropic off 应显式报错", False)
    except ValueError:
        check("anthropic effort 模型拒绝 off", True)


# ── 4. 能力校验：不支持档位显式报错（不静默降级）──────────────────────────────
def test_unsupported_raises():
    # 未知档位拼写
    try:
        map_reasoning("openai", "gpt-5", "ultra")
        check("未知档位拼写应报错", False)
    except ValueError:
        check("未知档位拼写显式报错", True)

    # 默认（未登记）模型只认 low/medium/high，传 max 应报错
    try:
        map_reasoning("anthropic", "some-unknown-model", MAX)
        check("未登记模型越界档位应报错", False)
    except ValueError as e:
        check("未登记模型越界档位显式报错", True, str(e))


# ── 5. provider 路由：构造出正确的 Model 子类 ─────────────────────────────────
def test_router_openai():
    m = build_model("openai", "gpt-5", "k", "https://x/v1", "high")
    cls = type(m).__module__ + "." + type(m).__name__
    check("openai → smolagents OpenAIModel", cls.endswith("OpenAIModel"), cls)
    # reasoning_effort 已注入 kwargs
    check("openai reasoning_effort 注入", m.kwargs.get("reasoning_effort") == "high",
          str(m.kwargs))


def test_router_anthropic():
    m = build_model("anthropic", "claude-opus-4-6", "k", "https://gw", "high")
    cls = type(m).__module__ + "." + type(m).__name__
    check("anthropic → AnthropicModel", cls.endswith("AnthropicModel"), cls)
    check("anthropic output_config 注入",
          m.reasoning_kwargs == {"output_config": {"effort": "high"}},
          str(m.reasoning_kwargs))


def test_router_unknown_provider():
    try:
        build_model("grok", "x", "k", "https://x", "high")
        check("未知 provider 应报错", False)
    except ValueError:
        check("未知 provider 显式报错", True)


# ── 6. timeout 默认：openai 300 / anthropic 600 ────────────────────────────────
def test_timeout_defaults():
    om = build_model("openai", "gpt-5", "k", "https://x/v1", "high")
    am = build_model("anthropic", "claude-opus-4-6", "k", "https://gw", "high")
    # OpenAI：timeout 落在 client_kwargs
    check("openai timeout=300", om.client_kwargs.get("timeout") == 300.0, str(om.client_kwargs))
    check("anthropic timeout=600", am.timeout == 600.0, str(am.timeout))


# ── 7. 能力表：supported_levels 按模型版本精确区分 xhigh/max ───────────────────
def test_capability_lookup():
    # claude-opus-4-6：支持 max，不支持 xhigh（xhigh 从 4.7 起）
    levels_46 = supported_levels("anthropic", "claude-opus-4-6")
    check("claude-opus-4-6 支持 max", MAX in levels_46)
    check("claude-opus-4-6 不支持 xhigh", XHIGH not in levels_46, str(levels_46))
    check("claude-opus-4-6 不支持 off", OFF not in levels_46)
    # claude-opus-4-7：支持 xhigh
    check("claude-opus-4-7 支持 xhigh", XHIGH in supported_levels("anthropic", "claude-opus-4-7"))
    # gpt-5：off 也支持
    check("gpt-5 支持 off", OFF in supported_levels("openai", "gpt-5"))
    # 带网关前缀也能识别
    check("网关前缀 claude-opus-4-6 识别", MAX in supported_levels("anthropic", "anthropic/claude-opus-4-6"))


# ── 8. 能力表：opus-4-6 + xhigh 显式报错（防止 400）──────────────────────────
def test_opus46_xhigh_rejected():
    try:
        map_reasoning("anthropic", "claude-opus-4-6", XHIGH)
        check("claude-opus-4-6 + xhigh 应显式报错", False)
    except ValueError:
        check("claude-opus-4-6 + xhigh 显式报错（避免服务端 400）", True)


tests = [
    test_levels_complete, test_openai_mapping, test_anthropic_mapping,
    test_unsupported_raises, test_router_openai, test_router_anthropic,
    test_router_unknown_provider, test_timeout_defaults, test_capability_lookup,
    test_opus46_xhigh_rejected,
]

if __name__ == "__main__":
    print("== test_model_factory ==")
    total = len(tests)
    for t in tests:
        t()
    print(f"\n{len(PASSED)} 项检查通过 / {total} 个测试:")
    for label in PASSED:
        # Windows GBK 终端打不出 ✓(U+2713),打印 ASCII 化版本避免 UnicodeEncodeError
        safe = label.encode("ascii", "replace").decode("ascii")
        print(f"  ok  {safe}")
    print("\nMODEL FACTORY TESTS PASSED")
