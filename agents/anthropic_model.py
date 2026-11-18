"""Anthropic 原生 /v1/messages 的 smolagents Model 适配器。

定位
----
smolagents 的 CodeAgent 不关心底层协议，只要传入的 Model 实现 ``generate``（非流式）/``generate_stream``
（流式）并返回统一的 :class:`smolagents.ChatMessage` 即可。本类让 Claude 走 **Anthropic 官方
SDK + /v1/messages**（而非被压平成 OpenAI-compatible chat/completions），从而保留
``output_config.effort`` / ``thinking`` / 原生流式 SSE 等 Claude 能力。

边界（与用户锁定的 A' 方案一致）
--------------------------------
- 本类只负责"调用 LLM"：消息格式转换、reasoning 注入、流式 delta 转换。
- **不实现 Anthropic 原生 tool_use/tool_result 状态机**——本项目三个 Agent 全是 CodeAgent，
  其主循环只读 ``ChatMessage.content`` 里的 Python 代码块，不传 ``tools_to_call_from``、不读
  ``tool_calls``（见 smolagents ``agents.py`` CodeAgent._step_stream）。``tools_to_call_from``
  在此仅作防御性接受并转成 Anthropic tools，但默认路径用不到。
- 无状态：每次 ``generate`` 只依赖传入 messages + 自身配置，不维护对话历史（CodeAgent 每步
  把全量历史喂进来）。
"""
import base64
import mimetypes
import os
from pathlib import Path
from typing import Any

from smolagents.models import (
    ApiModel,
    ChatMessage,
    ChatMessageStreamDelta,
    MessageRole,
    TokenUsage,
)


class AnthropicModel(ApiModel):
    """用 Anthropic Python SDK 直连自建 Anthropic-compatible /v1/messages 的 smolagents Model。

    Parameters
    ----------
    model_id : str
        网关接受的模型 slug（如 ``claude-opus-4-6``）。不再带 ``anthropic/`` 之类的路由前缀——
        provider 由 router 的显式字段决定。
    api_key, api_base : str
        自建 Anthropic-compatible 网关的密钥与 base_url。
    timeout : float
        单请求超时（秒）。Claude reasoning 任务偏长，默认 600。
    max_retries : int
        Anthropic SDK 客户端层的重试次数。排障期建议 0。
    max_tokens : int
        Anthropic 强制要求的最大输出 token 数。可通过 env ``ANTHROPIC_MAX_TOKENS`` 覆盖。
    **reasoning_kwargs
        由 :func:`agents.model_capabilities.map_reasoning` 产出的原生参数
        （``output_config=...`` 或 ``thinking=...``），每次请求注入。
    """

    def __init__(
        self,
        model_id: str,
        api_key: str,
        api_base: str,
        timeout: float = 600.0,
        max_retries: int = 0,
        max_tokens: int | None = None,
        **reasoning_kwargs: Any,
    ):
        # 注意：ApiModel.__init__ 内部会立即调 self.create_client()，故所有客户端构造
        # 所需属性必须先于 super().__init__() 赋值。
        self.api_key = api_key
        self.api_base = api_base
        self.timeout = timeout
        self.max_retries = max_retries
        self.max_tokens = int(
            max_tokens
            if max_tokens is not None
            else os.getenv("ANTHROPIC_MAX_TOKENS", "16384")
        )
        # reasoning_kwargs 形如 {"output_config": {"effort": "high"}} 或 {"thinking": {...}}
        # （或为空）。每次请求注入；单独存以便 to_dict 等场景可查。
        self.reasoning_kwargs = reasoning_kwargs or {}
        super().__init__(model_id=model_id, retry=False)

    # ── 客户端构造 ──────────────────────────────────────────────────────────
    def create_client(self):
        try:
            import anthropic
        except ModuleNotFoundError as e:
            raise ModuleNotFoundError(
                "AnthropicModel 需要 anthropic SDK：pip install 'anthropic>=0.121.0'"
            ) from e
        kwargs = {
            "api_key": self.api_key or None,
            "base_url": self.api_base or None,
            "timeout": self.timeout,
            "max_retries": self.max_retries,
        }
        return anthropic.Anthropic(**kwargs)

    # ── 消息格式转换：smolagents ChatMessage → Anthropic ──────────────────────
    def _split_system_and_messages(
        self, messages: list[ChatMessage | dict]
    ) -> tuple[str | None, list[dict]]:
        """把 smolagents 的消息列表转成 Anthropic 的 (system, messages)。

        - ``role=SYSTEM`` 抽出来拼成顶层 ``system``（Anthropic 的 system 不在 messages 里）。
        - ``TOOL_CALL``→assistant、``TOOL_RESPONSE``→user（对齐 smolagents
          ``tool_role_conversions``）。
        - 相邻同 role 合并（Anthropic 服务端本身也会合并连续同角色 turn；客户端合并是为
          便于阅读，不是 schema 硬性要求）。
        """
        system_parts: list[str] = []
        convo: list[tuple[str, list]] = []  # (role, content_blocks)
        for message in messages:
            if isinstance(message, dict):
                message = ChatMessage.from_dict(message)
            role = message.role
            content = message.content

            if role == MessageRole.SYSTEM:
                system_parts.append(self._content_to_text(content))
                continue
            # 角色归一：CodeAgent 不做原生 tool 往返，按 smolagents 约定把 tool 角色拍平。
            if role == MessageRole.TOOL_CALL:
                target_role = "assistant"
            elif role == MessageRole.TOOL_RESPONSE:
                target_role = "user"
            else:
                target_role = role.value if hasattr(role, "value") else str(role)

            blocks = self._content_to_anthropic_blocks(content)
            if convo and convo[-1][0] == target_role:
                # 合并相邻同 role（content 拼成 list）。
                convo[-1] = (target_role, convo[-1][1] + blocks)
            else:
                convo.append((target_role, blocks))

        # 统一 content 形态：单个 text block 退化成裸字符串（Anthropic 接受两者）。
        norm_messages = []
        for role, content in convo:
            if (
                len(content) == 1
                and isinstance(content[0], dict)
                and content[0].get("type") == "text"
            ):
                content = content[0]["text"]
            norm_messages.append({"role": role, "content": content})

        system = "\n\n".join(p for p in system_parts if p).strip() or None
        return system, norm_messages

    @staticmethod
    def _content_to_text(content: Any) -> str:
        """把 str / list-of-blocks 的 content 压成纯文本（供 system 合并）。"""
        if content is None:
            return ""
        if isinstance(content, str):
            return content
        parts = []
        if isinstance(content, list):
            for el in content:
                if isinstance(el, dict) and el.get("type") in ("text", None):
                    parts.append(str(el.get("text", "")))
                elif isinstance(el, str):
                    parts.append(el)
        return "\n".join(p for p in parts if p)

    @classmethod
    def _content_to_anthropic_blocks(cls, content: Any) -> list[Any]:
        """把 smolagents content 转成 Anthropic content block 列表。

        - str → [{"type":"text","text":s}]
        - list 内 ``{"type":"image","image":<bytes/path/b64/data-url/PIL>}`` → Anthropic image
          block（base64 source）；``{"type":"image_url",...}`` → base64 或 url source；
          ``{"type":"text",...}`` 透传。
        """
        if content is None:
            raise ValueError("Anthropic message content 不能为 None")
        if isinstance(content, str):
            if not content:
                raise ValueError("Anthropic text content 不能为空")
            return [{"type": "text", "text": content}]
        blocks = []
        if isinstance(content, list):
            for el in content:
                if isinstance(el, str):
                    if el:
                        blocks.append({"type": "text", "text": el})
                    continue
                if not isinstance(el, dict):
                    text = str(el)
                    if text:
                        blocks.append({"type": "text", "text": text})
                    continue
                etype = el.get("type")
                if etype == "image":
                    blocks.append(cls._image_to_anthropic(el))
                elif etype == "image_url":
                    # OpenAI 风格 image_url → base64（data URL）或 url source。
                    blocks.append(cls._image_url_to_anthropic(el))
                else:  # text / 未知 → 取 text 字段当文本
                    text = el.get("text", "")
                    if text:
                        blocks.append({"type": "text", "text": str(text)})
        if not blocks:
            raise ValueError("Anthropic message 至少需要一个非空 content block")
        return blocks

    @classmethod
    def _image_to_anthropic(cls, el: dict) -> dict:
        """smolagents image 元素 → Anthropic base64 image block。

        支持的 ``image`` 取值：PIL.Image（smolagents 标准形态，经 encode_image_base64 转 PNG）、
        bytes/bytearray、base64 字符串、``data:<mime>;base64,...`` data URL；``path`` 指向本地
        文件时读字节并按后缀推断 MIME。
        """
        from smolagents.utils import encode_image_base64  # 懒加载，避免无图像时强依赖

        raw = el.get("image")
        path = el.get("path")
        media = el.get("mime") or el.get("media_type")

        if path:
            payload = Path(path).read_bytes()
            b64 = base64.b64encode(payload).decode("ascii")
            if media is None:
                guessed = mimetypes.guess_type(str(path))[0]
                media = guessed or "image/png"
        elif isinstance(raw, (bytes, bytearray)):
            b64 = base64.b64encode(raw).decode("ascii")
        elif isinstance(raw, str) and raw.startswith("data:"):
            header, separator, b64 = raw.partition(",")
            if not separator or ";base64" not in header:
                raise ValueError("image data URL 必须是 base64 编码")
            if media is None:
                media = header[5:].split(";", 1)[0]  # data: 之后、; 之前
        elif isinstance(raw, str):
            b64 = raw  # 假定已是 base64
        elif raw is not None:
            # smolagents 标准图片内容是 PIL.Image（或带 .save() 的对象），转成 PNG base64。
            b64 = encode_image_base64(raw)
            media = media or "image/png"
        else:
            raise ValueError("image block 缺少可用的 image/path 字段")

        allowed = {"image/jpeg", "image/png", "image/gif", "image/webp"}
        if media not in allowed:
            raise ValueError(
                f"Anthropic 不支持 image media_type={media!r}；允许值：{sorted(allowed)}"
            )
        return {
            "type": "image",
            "source": {"type": "base64", "media_type": media, "data": b64},
        }

    @classmethod
    def _image_url_to_anthropic(cls, el: dict) -> dict:
        image_url = el.get("image_url")
        url = image_url.get("url", "") if isinstance(image_url, dict) else (image_url or "")
        if not url:
            raise ValueError("image_url block 缺少 url")
        if url.startswith("data:"):
            # data URL 复用 base64 转换逻辑（Anthropic URL source 仅面向 HTTP(S)）。
            return cls._image_to_anthropic({"image": url})
        if not url.startswith(("https://", "http://")):
            raise ValueError("Anthropic URL image source 必须使用 HTTP(S) URL")
        return {"type": "image", "source": {"type": "url", "url": url}}

    @staticmethod
    def _total_input_tokens(usage: Any) -> int:
        """计入 prompt caching：input + cache_creation + cache_read。"""
        return int(sum(
            (getattr(usage, f, 0) or 0)
            for f in ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")
        ))

    # ── 请求组装 ────────────────────────────────────────────────────────────
    def _build_request(
        self,
        messages: list[ChatMessage | dict],
        stop_sequences: list[str] | None = None,
        tools_to_call_from: list | None = None,
    ) -> dict:
        system, msgs = self._split_system_and_messages(messages)
        if not msgs:
            raise ValueError("Anthropic /v1/messages 要求至少一条非 system 消息")
        req = {
            "model": self.model_id,
            "max_tokens": self.max_tokens,
            "messages": msgs,
        }
        if system:
            req["system"] = system
        if stop_sequences:
            req["stop_sequences"] = list(stop_sequences)
        if tools_to_call_from:
            req["tools"] = [self._tool_to_anthropic(t) for t in tools_to_call_from]
        req.update(self.reasoning_kwargs)
        return req

    @staticmethod
    def _tool_to_anthropic(tool) -> dict:
        """smolagents Tool → Anthropic tool 定义（防御性；CodeAgent 路径不传 tools）。"""
        name = getattr(tool, "name", None)
        description = getattr(tool, "description", "")
        inputs = getattr(tool, "inputs", {}) or {}
        properties = {}
        required = []
        for key, spec in inputs.items():
            spec = dict(spec)
            nullable = bool(spec.pop("nullable", False))
            if not nullable:
                required.append(key)
            if spec.get("type") == "any":
                spec["type"] = "string"
            properties[key] = spec
        return {
            "name": name,
            "description": description,
            "input_schema": {
                "type": "object",
                "properties": properties,
                "required": required,
            },
        }

    # ── 非流式生成 ──────────────────────────────────────────────────────────
    def generate(
        self,
        messages: list[ChatMessage | dict],
        stop_sequences: list[str] | None = None,
        response_format: dict | None = None,
        tools_to_call_from: list | None = None,
        **kwargs,
    ) -> ChatMessage:
        # response_format（结构化输出）是 OpenAI 概念，Anthropic 无对应字段，忽略不报错。
        request = self._build_request(messages, stop_sequences, tools_to_call_from)
        self._apply_rate_limit()
        response = self.client.messages.create(**request)
        text = "".join(
            getattr(block, "text", "")
            for block in response.content
            if getattr(block, "type", None) == "text"
        )
        usage = response.usage
        return ChatMessage(
            role=MessageRole.ASSISTANT,
            content=text,
            token_usage=TokenUsage(
                input_tokens=self._total_input_tokens(usage),
                output_tokens=getattr(usage, "output_tokens", 0),
            ),
            raw=response,
        )

    # ── 流式生成 ────────────────────────────────────────────────────────────
    def generate_stream(
        self,
        messages: list[ChatMessage | dict],
        stop_sequences: list[str] | None = None,
        response_format: dict | None = None,
        tools_to_call_from: list | None = None,
        **kwargs,
    ):
        """Anthropic SSE → smolagents ``ChatMessageStreamDelta`` 流。

        CodeAgent 的流式消费端（smolagents ``agents.py``）收集全部 delta 后调
        ``agglomerate_stream_deltas`` 合成最终 ChatMessage，故这里只需 yield：
        - 文本增量：``ChatMessageStreamDelta(content=<text>)``
        - 末尾 usage：``ChatMessageStreamDelta(content="", token_usage=...)``
        （token usage 在 message_start/message_delta 事件里分别给 input/output_tokens。）
        """
        request = self._build_request(messages, stop_sequences, tools_to_call_from)
        self._apply_rate_limit()
        input_tokens = 0
        output_tokens = 0
        with self.client.messages.stream(**request) as stream:
            for event in stream:
                etype = getattr(event, "type", None)
                if etype == "message_start":
                    msg = getattr(event, "message", None)
                    if msg is not None and getattr(msg, "usage", None) is not None:
                        input_tokens = self._total_input_tokens(msg.usage)
                elif etype == "content_block_delta":
                    delta = getattr(event, "delta", None)
                    dtype = getattr(delta, "type", None)
                    if dtype == "text_delta":
                        chunk = getattr(delta, "text", "")
                        if chunk:
                            yield ChatMessageStreamDelta(content=chunk)
                elif etype == "message_delta":
                    usage = getattr(event, "usage", None)
                    if usage is not None:
                        output_tokens = getattr(usage, "output_tokens", 0) or 0
        # usage 单独成一条 delta（content 为空），与 OpenAIModel.generate_stream 的做法一致。
        yield ChatMessageStreamDelta(
            content="",
            token_usage=TokenUsage(
                input_tokens=input_tokens, output_tokens=output_tokens
            ),
        )

    # ── 序列化辅助（to_dict 用，避免泄漏 key） ────────────────────────────────
    def to_dict(self) -> dict:
        base = super().to_dict()
        base.pop("api_key", None)
        base.update(
            {
                "api_base": self.api_base,
                "timeout": self.timeout,
                "max_retries": self.max_retries,
                "max_tokens": self.max_tokens,
            }
        )
        return base
