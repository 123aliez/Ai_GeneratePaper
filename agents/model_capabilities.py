"""统一推理深度（reasoning_level）语义层与 provider 原生参数映射。

本模块是"LLM 调用框架"的一部分，不含任何 Agent 业务逻辑。

设计目标
--------
smolagents 的三个 Agent（draft/review/manager）可能由不同 provider（OpenAI / Anthropic）
驱动，且各自对"思考深度"用完全不同的原生参数：

- OpenAI（Chat Completions 兼容接口） → 顶层 ``reasoning_effort``
- Anthropic（``/v1/messages``）
    * effort 模型（claude-opus-4-6/4-8/5、sonnet-4-6/5 …） → ``output_config={"effort": ...}``
    * 旧 extended-thinking 模型                          → ``thinking={"type":"enabled",
                                                          "budget_tokens": N}``（本模块预留，默认走 effort）

上层只暴露一个**统一的语义档位** ``reasoning_level``，由本模块翻译成 provider 原生参数。
档位是"行为级意图"，不是严格等价的 token 数。

档位
----
``off / low / medium / high / xhigh / max``（共 6 档）。

能力校验
--------
不同模型支持的档位集合不同（例如 Claude effort 模型无 ``off``）。本模块维护一张能力表，
传入模型**不支持的档位时显式抛 ``ValueError``**——绝不静默降级，避免"你以为跑了 max，
实际跑成 high"。

参考
----
- Claude effort:    https://platform.claude.com/docs/en/build-with-claude/effort
- Claude thinking:  https://platform.claude.com/docs/en/build-with-claude/thinking
- OpenAI reasoning: https://developers.openai.com/api/docs/guides/reasoning
"""

# ── 统一语义档位 ──────────────────────────────────────────────────────────────
OFF = "off"
LOW = "low"
MEDIUM = "medium"
HIGH = "high"
XHIGH = "xhigh"
MAX = "max"

ALL_LEVELS = (OFF, LOW, MEDIUM, HIGH, XHIGH, MAX)

# OpenAI 顶层 reasoning_effort 取值（user 确认当前 GPT-5.x 支持全部 6 档；off→none）。
_OPENAI_EFFORT = {
    OFF: "none",
    LOW: "low",
    MEDIUM: "medium",
    HIGH: "high",
    XHIGH: "xhigh",
    MAX: "max",
}

# Anthropic output_config.effort 取值（effort 模型不支持 off）。
_CLAUDE_EFFORT = {
    LOW: "low",
    MEDIUM: "medium",
    HIGH: "high",
    XHIGH: "xhigh",
    MAX: "max",
}

# ── 能力表：(provider, model_id 前缀) → 支持的档位集合 ────────────────────────
# 升级模型 / 切换网关模型时，先查官方 model page，再在此更新。未登记的 (provider,model)
# 默认保守地只认 {LOW, MEDIUM, HIGH}，并对越界档位显式报错，逼迫使用者显式登记。
#
# Anthropic effort 档位**按具体模型版本**区分（xhigh/max 并非全家族通用），故此处登记到
# 版本号；匹配时取最长前缀（见 supported_levels）。依据：
#   https://platform.claude.com/docs/en/build-with-claude/effort  的 effort levels 表
_DEFAULT_LEVELS = frozenset({LOW, MEDIUM, HIGH})

CAPABILITIES = {
    # OpenAI：GPT-5.x 六档全支持（含 none/off）。
    ("openai", "gpt-5"): frozenset(ALL_LEVELS),
    # Anthropic —— Opus
    ("anthropic", "claude-opus-4-5"): frozenset({LOW, MEDIUM, HIGH}),
    ("anthropic", "claude-opus-4-6"): frozenset({LOW, MEDIUM, HIGH, MAX}),
    ("anthropic", "claude-opus-4-7"): frozenset({LOW, MEDIUM, HIGH, XHIGH, MAX}),
    ("anthropic", "claude-opus-4-8"): frozenset({LOW, MEDIUM, HIGH, XHIGH, MAX}),
    ("anthropic", "claude-opus-5"): frozenset({LOW, MEDIUM, HIGH, XHIGH, MAX}),
    # Anthropic —— Sonnet
    ("anthropic", "claude-sonnet-4-6"): frozenset({LOW, MEDIUM, HIGH, MAX}),
    ("anthropic", "claude-sonnet-5"): frozenset({LOW, MEDIUM, HIGH, XHIGH, MAX}),
    # Anthropic —— 其它
    ("anthropic", "claude-fable-5"): frozenset({LOW, MEDIUM, HIGH, XHIGH, MAX}),
    ("anthropic", "claude-mythos-preview"): frozenset({LOW, MEDIUM, HIGH, MAX}),
    ("anthropic", "claude-mythos-5"): frozenset({LOW, MEDIUM, HIGH, XHIGH, MAX}),
}


def supported_levels(provider: str, model_id: str) -> frozenset:
    """返回某 (provider, model) 支持的 reasoning_level 集合。

    匹配规则：取 model_id 去掉网关前缀后的 slug，在能力表里找 (provider 一致 且 slug 以
    表中 model 前缀开头) 的项，**取最长前缀匹配**（最具体的族优先，如 ``claude-opus`` 优先于
    ``claude``）。查不到返回保守默认。
    """
    provider = (provider or "").strip().lower()
    slug = _strip_gateway_prefix((model_id or "").strip().lower())
    best_key = None
    best_len = -1
    for (prov, prefix), levels in CAPABILITIES.items():
        if prov != provider:
            continue
        if prefix and slug.startswith(prefix) and len(prefix) > best_len:
            best_key = (prov, prefix)
            best_len = len(prefix)
    if best_key is not None:
        return CAPABILITIES[best_key]
    return _DEFAULT_LEVELS


def map_reasoning(provider: str, model_id: str, level: str) -> dict:
    """把统一 ``reasoning_level`` 翻译成 provider 原生请求参数。

    返回的 dict 直接 ``.update()`` 进 OpenAI/Anthropic 的请求 kwargs。

    抛错策略
    --------
    - ``level`` 不在 :data:`ALL_LEVELS` → ``ValueError``（拼写错误，属程序员 bug）。
    - ``level`` 在全集中但该模型不支持（如 Claude 的 ``off``）→ ``ValueError``（显式报错，
      列出该模型支持的档位，绝不静默降级）。

    OpenAI  → ``{"reasoning_effort": <str>}``
    Anthropic:
        - ``off``  → ``{"thinking": {"type": "disabled"}}``（仅能力允许时；否则上面已报错）
        - 其它     → ``{"output_config": {"effort": <str>}}``
    """
    level = (level or "").strip().lower()
    if level not in ALL_LEVELS:
        raise ValueError(
            f"未知的 reasoning_level={level!r}，合法值：{list(ALL_LEVELS)}"
        )

    allowed = supported_levels(provider, model_id)
    if level not in allowed:
        raise ValueError(
            f"reasoning_level={level!r} 不被 (provider={provider!r}, model={model_id!r}) 支持；"
            f"该模型支持：{sorted(allowed)}"
        )

    provider = (provider or "").strip().lower()
    if provider == "openai":
        return {"reasoning_effort": _OPENAI_EFFORT[level]}
    if provider == "anthropic":
        if level == OFF:
            return {"thinking": {"type": "disabled"}}
        return {"output_config": {"effort": _CLAUDE_EFFORT[level]}}
    raise ValueError(f"未知 provider={provider!r}，合法值：'openai' / 'anthropic'")


def _strip_gateway_prefix(model_id: str) -> str:
    """去掉 litellm/网关路由前缀（``anthropic/``、``openai/`` …）。

    provider 由显式的 ``*_PROVIDER`` 字段决定，model_id 里的前缀只是网关 slug 的一部分，
    不再用于推断 provider（避免歧义）。大小写不敏感。
    """
    for prefix in ("anthropic/", "openai/", "xai/", "gemini/", "groq/"):
        if model_id.startswith(prefix):
            return model_id[len(prefix):]
    return model_id
