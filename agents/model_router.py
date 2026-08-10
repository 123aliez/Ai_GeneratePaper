"""模型工厂（Model Router）：按 provider 把统一配置分发给具体 Model 实现。

本模块是"LLM 调用框架"的唯一对外入口。``config.py`` 的三个 getter 只调
:func:`build_model`；下游（agents.py / orchestrator.py / run.py）完全不感知 provider 差异。

provider 来源
-------------
provider 由 ``.env`` 的显式字段（``*_PROVIDER``）决定，**不再靠 model_id 前缀推断**——
因为同一个模型可能同时暴露 OpenAI-compatible 与 Anthropic-compatible 两套 endpoint，
用前缀猜 provider 会制造歧义。

参数职责
--------
- ``provider`` ：API 协议 / Adapter 类型（openai | anthropic）
- ``model_id`` ：服务端模型 slug（网关真正接受的字符串）
- ``reasoning_level`` ：统一语义档位（off|low|medium|high|xhigh|max），由 capabilities 翻译
- ``timeout``/``max_retries``：运行时策略（OpenAI 默认 300s、Claude 600s、排障期 retries=0）
"""
from typing import Any

from .model_capabilities import map_reasoning

# 各 provider 的默认单请求超时（秒）。Claude reasoning 任务更长。
_DEFAULT_TIMEOUT = {"openai": 300.0, "anthropic": 600.0}


def build_model(
    provider: str,
    model_id: str,
    api_key: str,
    api_base: str,
    reasoning_level: str,
    timeout: float | None = None,
    max_retries: int = 0,
    **extra: Any,
):
    """构造一个 smolagents Model 实例。

    返回类型对下游统一为 ``smolagents.models.Model`` 的子类，故 ``config.py`` 的 getter、
    ``agents.py`` 的 Agent 装配无需区分 provider。

    任何不被该 (provider, model) 支持的 ``reasoning_level`` 会在本函数内（经
    :func:`map_reasoning`）**显式抛 ValueError**，绝不静默降级。

    Parameters
    ----------
    provider : str
        ``"openai"`` 走 smolagents ``OpenAIModel``（openai 官方 SDK）；``"anthropic"`` 走
        本项目 :class:`agents.anthropic_model.AnthropicModel`（anthropic 官方 SDK + /v1/messages）。
    model_id : str
        网关真实模型 slug。
    api_key, api_base : str
        网关密钥与 base_url。
    reasoning_level : str
        统一推理档位。
    timeout : float, optional
        单请求超时；不传则用 provider 默认（OpenAI 300 / Claude 600）。
    max_retries : int
        SDK 客户端层重试次数（同时关掉 smolagents 的 rate-limit 重试器）。默认 0。
    **extra
        透传给底层 Model 的额外构造参数（如 ``max_tokens``）。
    """
    provider = (provider or "").strip().lower()
    reasoning_kwargs = map_reasoning(provider, model_id, reasoning_level)
    effective_timeout = timeout if timeout is not None else _DEFAULT_TIMEOUT.get(provider, 300.0)

    if provider == "openai":
        from smolagents import OpenAIModel

        # client_kwargs 透传给 openai SDK 客户端：timeout 控单请求、max_retries 控重试。
        # retry=False 关掉 smolagents ApiModel 的 rate-limit 重试器（重试交由 SDK 客户端层）。
        return OpenAIModel(
            model_id=model_id,
            api_base=api_base or None,
            api_key=api_key or None,
            client_kwargs={
                "timeout": effective_timeout,
                "max_retries": max_retries,
            },
            retry=False,
            **reasoning_kwargs,
            **extra,
        )

    if provider == "anthropic":
        from .anthropic_model import AnthropicModel

        return AnthropicModel(
            model_id=model_id,
            api_key=api_key,
            api_base=api_base,
            timeout=effective_timeout,
            max_retries=max_retries,
            **reasoning_kwargs,
            **extra,
        )

    raise ValueError(
        f"未知 provider={provider!r}，合法值：'openai' / 'anthropic'"
    )
