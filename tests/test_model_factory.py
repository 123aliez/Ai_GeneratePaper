"""模型调用框架（三种 provider + reasoning 映射）的离线验证。

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


# ── 5. provider 路由：构造出正确的底层 Model（被 ResilientModel 包裹）────────
# build_model 现在统一把底层 Model 包进 ResilientModel 再返回，所以 type(m) 总是
# ResilientModel；要验底层路由正确，得看 m.base。
def test_router_openai():
    m = build_model("openai", "gpt-5", "k", "https://x/v1", "high")
    check("build_model 外层是 ResilientModel",
          type(m).__name__ == "ResilientModel", type(m).__name__)
    base = m.base
    cls = type(base).__module__ + "." + type(base).__name__
    check("openai 底层 → smolagents OpenAIModel", cls.endswith("OpenAIModel"), cls)
    # reasoning_effort 已注入 kwargs
    check("openai reasoning_effort 注入", base.kwargs.get("reasoning_effort") == "high",
          str(base.kwargs))


def test_router_anthropic():
    m = build_model("anthropic", "claude-opus-4-6", "k", "https://gw", "high")
    base = m.base
    cls = type(base).__module__ + "." + type(base).__name__
    check("anthropic 底层 → AnthropicModel", cls.endswith("AnthropicModel"), cls)
    check("anthropic output_config 注入",
          base.reasoning_kwargs == {"output_config": {"effort": "high"}},
          str(base.reasoning_kwargs))


def test_router_unknown_provider():
    try:
        build_model("grok", "x", "k", "https://x", "high")
        check("未知 provider 应报错", False)
    except ValueError:
        check("未知 provider 显式报错", True)


# ── 6. timeout 默认：openai 300 / anthropic 600（透传到底层）──────────────────
def test_timeout_defaults():
    om = build_model("openai", "gpt-5", "k", "https://x/v1", "high").base
    am = build_model("anthropic", "claude-opus-4-6", "k", "https://gw", "high").base
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


def test_router_claude_cli_without_credentials():
    from unittest.mock import patch
    with patch("agents.claude_cli_model.subprocess.Popen") as popen, \
            patch("agents.claude_cli_model.tempfile.TemporaryDirectory") as temporary, \
            patch.dict("os.environ", {"CLAUDE_CLI_PATH": "/fake/claude"}):
        m = build_model("claude_cli", "sonnet", "", "", "max", owner="Draft")
        check("CLI 外层仍是 ResilientModel", type(m).__name__ == "ResilientModel")
        check("CLI 无 key/base 也能构造", type(m.base).__name__ == "ClaudeCliModel")
        check("CLI 默认总超时 600 秒", m.base.timeout == 600.0)
        check("CLI effort 与可执行路径传入", m.base.effort == "max" and m.base.cli_path == "/fake/claude")
        direct = build_model("claude_cli", "full/model-id", "ignored", "https://ignored", "low",
                             timeout=123, retry_enabled=False)
        check("CLI 可关闭包装且保留模型原文", type(direct).__name__ == "ClaudeCliModel"
              and direct.model_id == "full/model-id" and direct.timeout == 123)
        check("CLI 构造无子进程及临时目录副作用", not popen.called and not temporary.called)


def test_claude_cli_effort_mapping():
    for model in ("sonnet", "opus", "fable", "claude-opus-4-6"):
        check(f"CLI {model} 支持全部五档", supported_levels("claude_cli", model)
              == frozenset({LOW, MEDIUM, HIGH, XHIGH, MAX}))
        for level in (LOW, MEDIUM, HIGH, XHIGH, MAX):
            check(f"CLI {model}/{level} → effort", map_reasoning("claude_cli", model, level)
                  == {"effort": level})
    for level in (OFF, "ultra", ""):
        try:
            build_model("claude_cli", "sonnet", "", "", level)
            check(f"CLI 拒绝档位 {level!r}", False)
        except ValueError:
            check(f"CLI 拒绝档位 {level!r}", True)


def test_all_role_getters_can_use_claude_cli():
    import config
    from unittest.mock import patch
    for prefix, getter in (("DRAFT", config.get_draft_model), ("REVIEW", config.get_review_model),
                           ("MANAGER", config.get_manager_model)):
        with patch.multiple(config, **{f"{prefix}_PROVIDER": "claude_cli", f"{prefix}_MODEL": "sonnet",
                                       f"{prefix}_API_KEY": "", f"{prefix}_API_BASE": "",
                                       f"{prefix}_REASONING_LEVEL": "high", "MODEL_TIMEOUT": "",
                                       "LLM_RETRY_ENABLED": True}), \
                patch("agents.claude_cli_model.subprocess.Popen") as popen:
            model = getter()
            check(f"{prefix} getter 路由到 CLI", type(model.base).__name__ == "ClaudeCliModel"
                  and model.base.owner.upper() == prefix and model.base.timeout == 600.0)
            check(f"{prefix} getter 不启动 CLI", not popen.called)


tests = [
    test_levels_complete, test_openai_mapping, test_anthropic_mapping,
    test_unsupported_raises, test_router_openai, test_router_anthropic,
    test_router_unknown_provider, test_timeout_defaults, test_capability_lookup,
    test_opus46_xhigh_rejected, test_router_claude_cli_without_credentials,
    test_claude_cli_effort_mapping, test_all_role_getters_can_use_claude_cli,
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
