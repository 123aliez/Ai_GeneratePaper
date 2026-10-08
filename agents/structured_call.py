"""结构化输出单轮调用 —— review JSON 专用路径(移植自 MathModel_Paper 演进点①)。

MathModel_Paper 的实战教训(runner.py docstring):CodeAgent 会把模型输出的
JSON 围栏当代码执行,失败后错误回填上下文反复重试,结构化解析全军覆没。
本项目的对应病灶:Stage 2 的 review-v1.json 靠"JSON 校验失败 → 删产物 →
强制重跑整个 CodeAgent"事后补救(orchestrator C3 分支),一次事故烧掉
整轮 CodeAgent 的 token。

本模块给"输出本质是结构化数据"的任务一条旁路:直接调 model.generate()
单轮完成,不进 CodeAgent 循环。重试仍由 ResilientModel 全权负责(唯一
重试 owner),本层不再叠加。

接口约定(与 orchestrator 的 run_agent_stage 对齐):
- run_structured_call(model, agent_name, system, user, out_path, schema_hint)
  → dict | None:单轮生成、抽取 JSON、写产物文件;解析失败返回 None,
  由调用方决定降级路径(orchestrator 保持 C3 的强制重跑作为兜底)。
- model 参数接收 smolagents Model(Real 的或测试里的 stub 都行),
  只要求它有 .generate(messages) → ChatMessage(.content 是文本)。
"""

import os
import sys
from pathlib import Path


def _extract_json(text: str):
    """从可能带噪声的输出里抽 JSON(围栏优先,外层大括号兜底)。"""
    if not text:
        return None
    import re
    import json
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S)
    candidates = []
    if fenced:
        candidates.append(fenced.group(1))
    first, last = text.find("{"), text.rfind("}")
    if first != -1 and last > first:
        candidates.append(text[first:last + 1])
    for candidate in candidates:
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            cleaned = re.sub(r"[\x00-\x1f]", " ", candidate)
            try:
                return json.loads(cleaned)
            except json.JSONDecodeError:
                continue
    return None


def _emit(owner: str, action: str, detail: str = "") -> None:
    # 与 run_agent_stage 的行格式一致,进度过滤器能照常解析。
    line = f"[{owner:<8.8}] {action:<11} | {detail}" if detail else \
           f"[{owner:<8.8}] {action:<11}"
    sys.stdout.write(line + "\n")
    sys.stdout.flush()


def run_structured_call(model, agent_name: str, system: str, user: str,
                        out_path: str, schema_hint: str,
                        validate=None) -> dict | None:
    """单轮生成一个 JSON 产物。

    - messages 直接构造 SYSTEM + USER(不经 CodeAgent,模型输出不会被当代码执行);
    - 写 out_path(原子替换);
    - 成功返回解析后的 dict,解析失败返回 None(不抛——调用方有降级路径)。
    - validate(可选)在落盘前校验解析结果(review-v1.json 的 must_fix schema 等):
      返回 False 视同解析失败,不落盘、返回 None——语法合法但 schema 错的
      对象不该被当成功产物。
    模型调用异常向上抛:ResilientModel 已重试过,这里再包一层等于放大。
    """
    from smolagents.models import ChatMessage, MessageRole

    messages = [
        ChatMessage(role=MessageRole.SYSTEM, content=system),
        ChatMessage(role=MessageRole.USER, content=user),
    ]
    _emit(agent_name, "structured", f"single-turn JSON call → {out_path}")
    response = model.generate(messages)
    text = getattr(response, "content", "") or ""
    data = _extract_json(text)
    if data is None or (validate is not None and not validate(data)):
        _emit(agent_name, "structured-fail",
              "output was not usable JSON; falling back")
        return None
    path = Path(out_path)
    tmp = path.with_suffix(path.suffix + f".{os.getpid()}.tmp")
    import json
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)
    _emit(agent_name, "write_file", os.path.basename(out_path))
    return data
