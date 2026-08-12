"""模型工厂（Model Router）：按 provider 把统一配置分发给具体 Model 实现。

本模块是"LLM 调用框架"的唯一对外入口。``config.py`` 的三个 getter 只调
:func:`build_model`；下游（agents.py / orchestrator.py / run.py）完全不感知 provider 差异，
也完全不感知"瞬时重试在哪一层"。

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
- ``timeout``/``max_retries``：底层 SDK 客户端的单请求超时与重试次数
- ``retry_schedule`` ：ResilientModel 的瞬时重试计划（见 retry_policy.RetrySchedule）

重试归属（midstream plan v2 §5）
--------------------------------
瞬时网络重试**只由 ResilientModel 一层负责**：
    SDK max_retries = 0        （底层客户端不重试）
    smolagents retryer = OFF   （ApiModel retry=False）
    ResilientModel = ON        （唯一瞬时重试 owner）
    run_agent_stage = 最后兜底 （只对 TransientModelUnavailable 重跑，4 次纯兜底）

因此 ``build_model`` 永远把底层 Model 包进 ``ResilientModel`` 再返回——Agent 层拿到的
永远是 ResilientModel，对下游完全透明。
"""
from typing import Any

from smolagents.models import Model

from .model_capabilities import map_reasoning
from .retry_policy import RetrySchedule
from .resilient_model import ResilientModel

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
    retry_schedule: RetrySchedule | None = None,
    retry_enabled: bool = True,
    owner: str = "Agent",
    **extra: Any,
) -> Model:
    """构造一个（可选地）被 ResilientModel 包裹的 smolagents Model 实例。

    返回类型对下游统一为 ``smolagents.models.Model`` 的子类，故 ``config.py`` 的 getter、
    ``agents.py`` 的 Agent 装配无需区分 provider，也无需感知瞬时重试——重试全部在
    ResilientModel 内部完成，Agent 层只看到"一次成功的 model call"。

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
        底层 SDK 单请求超时；不传则用 provider 默认（OpenAI 300 / Claude 600）。
    max_retries : int
        **底层 SDK 客户端**的重试次数。瞬时重试已交给 ResilientModel，这里应为 0
        （见模块 docstring 的重试归属）。``retry_enabled=True`` 时若传入非 0 会强制
        改 0 并告警——避免 ResilientModel 3 次 attempt × SDK N 次重试的 amplification。
    retry_schedule : RetrySchedule, optional
        ResilientModel 的重试计划。``retry_enabled=True`` 且为 None 时用默认
        （3 次、0.8/2.0s 退避）。
    retry_enabled : bool
        是否包裹 ResilientModel。``False`` 时直接返回底层 Model（退化到改造前，
        便于排障对照）。此时 ``max_retries`` 恢复语义（底层 SDK 自行重试）。
    owner : str
        终端日志的 owner 标签（Draft / Review / Manager），透传给 ResilientModel。
    **extra
        透传给底层 Model 的额外构造参数（如 ``max_tokens``）。
    """
    provider = (provider or "").strip().lower()
    reasoning_kwargs = map_reasoning(provider, model_id, reasoning_level)
    effective_timeout = timeout if timeout is not None else _DEFAULT_TIMEOUT.get(provider, 300.0)

    # 重试归属唯一化：ResilientModel 开启时，底层 SDK max_retries 必须为 0，
    # 否则一次 logical call 最坏会 = ResilientModel attempts × SDK retries 次网络请求。
    if retry_enabled and max_retries != 0:
        print(f"[model-rtr] notice     | retry_enabled=True 但 max_retries={max_retries}，"
              f"强制改为 0（避免与 ResilientModel 叠加重试）", flush=True)
        max_retries = 0

    if provider == "openai":
        from smolagents import OpenAIModel

        base = OpenAIModel(
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
    elif provider == "anthropic":
        from .anthropic_model import AnthropicModel

        base = AnthropicModel(
            model_id=model_id,
            api_key=api_key,
            api_base=api_base,
            timeout=effective_timeout,
            max_retries=max_retries,
            **reasoning_kwargs,
            **extra,
        )
    else:
        raise ValueError(
            f"未知 provider={provider!r}，合法值：'openai' / 'anthropic'"
        )

    # retry_enabled=False：退化到改造前，直接返回底层 Model（排障对照用）。
    if not retry_enabled:
        return base

    # 统一包一层 ResilientModel：瞬时重试的唯一 owner。Agent 层对此无感。
    schedule = retry_schedule if retry_schedule is not None else RetrySchedule()
    return ResilientModel(base, schedule, owner=owner, provider=provider)
