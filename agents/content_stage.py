"""纯内容阶段:模型只产出文本,读文件、写文件、校验、重试全部由脚本完成。

为什么不再用带工具的 CodeAgent:
- 模型要自己调 read_file/write_file,还要按 `<code>` 格式回复、最后调 final_answer。
  真实运行里这些环节反复出问题:回复格式不对、没写出文件、把读文件工具的页眉抄进正文。
- 这些都不是内容质量问题,而是"让模型做编排"带来的。

做法:
1. 脚本提供本阶段材料(<documents>)，通常附原文，Claude CLI 可选按目录查阅;
2. 模型只返回文本,每个产物放在一个 `<file name="...">...</file>` 块里;
3. 脚本解析、校验文件名(必须是本阶段声明的产物)、原子写入本章文件夹;
4. 缺产物 → 脚本带着缺哪几个再请求一次(最多 rounds 轮),而不是整段重跑。

模型需要查文献原文时,回复里写 `<lookup key="键">查询词</lookup>`,脚本代查并把摘录
放回对话再请求一次(受次数限制,每次查询记入 fulltext-lookups.md)。
"""

from __future__ import annotations

import os
import re
import sys
from dataclasses import dataclass, field
from html import escape
from pathlib import Path
from typing import Callable

FILE_RE = re.compile(r'<file\s+name="([^"\n]+)"\s*>\n?(.*?)\n?</file>', re.S)
LOOKUP_RE = re.compile(r'<lookup\s+key="([^"\n]+)"\s*>(.*?)</lookup>', re.S)
SAFE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")

# 程序专属文件:模型即使声明了也写不了(与 tools.PROGRAM_OWNED 保持一致)
PROGRAM_OWNED = {".stage-manifest.json", "verification.md", "brief.md", "numbers-used.json",
                 "fulltext-lookups.md"}

DEFAULT_DOC_CHARS = 120_000


@dataclass
class Doc:
    """放进提示词的一份材料。path 与 text 二选一。"""
    name: str
    path: str | os.PathLike | None = None
    text: str | None = None
    max_chars: int | None = None

    def load(self) -> str | None:
        if self.text is not None:
            body = self.text
        elif self.path is not None and Path(self.path).is_file():
            body = Path(self.path).read_text(encoding="utf-8-sig", errors="replace")
        else:
            return None
        limit = self.max_chars or DEFAULT_DOC_CHARS
        if len(body) > limit:
            body = body[:limit] + f"\n[TRUNCATED: {len(body) - limit} more characters omitted]"
        return body


class ContentAgent:
    """一个角色 = 模型 + 系统提示词；阶段和产物由脚本管理。"""

    _offline_test_stub = False

    def __init__(self, model, instructions: str, owner: str):
        self.model = model
        self.instructions = instructions
        self.owner = owner

    def run_stage(self, task: str, docs: list, deliverables: list[str], out_dir, **kw):
        """编排器调用的统一入口;测试里的假角色实现同名方法即可替换。"""
        return run_content_stage(self, task, docs, deliverables, out_dir, **kw)


@dataclass
class StageOutcome:
    written: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
    rounds: int = 0
    reply: str = ""

    def __str__(self) -> str:
        return f"wrote {', '.join(self.written) or '-'}" + \
            (f"; missing {', '.join(self.missing)}" if self.missing else "")


# ── 提示词与解析 ───────────────────────────────────────────────────────

def build_user_message(task: str, docs: list[Doc], deliverables: list[str],
                       *, material_files: dict[str, str] | None = None) -> str:
    parts = ["<documents>"]
    for index, doc in enumerate(docs, start=1):
        body = doc.load()
        if body is None:
            parts.append(f'<document name="{doc.name}" status="missing"/>')
        elif material_files is not None:
            filename = f"{index:02d}.md"
            material_files[filename] = body
            parts.append(f'<document name="{escape(doc.name)}" path="materials/{filename}"/>')
        else:
            parts.append(f'<document name="{doc.name}">\n{body}\n</document>')
    parts.append("</documents>")
    parts.append(f"<task>\n{task.strip()}\n</task>")
    names = ", ".join(deliverables)
    parts.append(
        "<deliverables>\n"
        "Return every deliverable as its own block, exactly in this form, with the complete "
        "file content inside:\n"
        '<file name="NAME">\n(complete content)\n</file>\n'
        f"Deliverables (these names only): {names}\n"
        "The documents above are read-only inputs, never deliverables. Put nothing outside the "
        "blocks except, if needed, <lookup> requests.\n"
        "</deliverables>")
    return "\n\n".join(parts)


def _strip_outer_fence(text: str) -> str:
    m = re.fullmatch(r"\s*```[a-zA-Z]*\n(.*?)\n```\s*", text, re.S)
    return m.group(1) if m else text


def parse_file_blocks(reply: str, allowed: list[str]) -> dict[str, str]:
    """从回复里取出声明过的产物。后出现的同名块覆盖先出现的。"""
    found: dict[str, str] = {}
    for name, body in FILE_RE.findall(reply or ""):
        name = name.strip()
        if name in allowed and body.strip():
            found[name] = body.rstrip("\n") + "\n"
    if not found and len(allowed) == 1 and (reply or "").strip() and "<file" not in reply \
            and "<lookup" not in reply:
        # 只要一个产物、模型直接给了正文(没包块):接受,去掉可能的整体围栏
        found[allowed[0]] = _strip_outer_fence(reply).strip("\n") + "\n"
    return found


def _write_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".{os.getpid()}.tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def _call_model(model, messages: list, **kwargs) -> str:
    """取完整文本。优先走流式接口(长连接不会被网关空闲超时切断)。"""
    from smolagents.models import ChatMessage, MessageRole
    msgs = [ChatMessage(role=MessageRole(m["role"]), content=m["content"]) for m in messages]
    if hasattr(model, "generate_stream"):
        chunks = []
        for delta in model.generate_stream(msgs, **kwargs):
            piece = getattr(delta, "content", None)
            if piece:
                chunks.append(piece)
        return "".join(chunks)
    return getattr(model.generate(msgs, **kwargs), "content", "") or ""


def run_content_stage(agent: ContentAgent, task: str, docs: list[Doc],
                      deliverables: list[str], out_dir, *, rounds: int = 3,
                      lookup: Callable[[str, str], str] | None = None,
                      max_lookup_rounds: int = 2, max_lookups: int = 4,
                      label: str = "", on_write: Callable[[str], None] | None = None) -> StageOutcome:
    """跑一个纯内容阶段并把产物写进 out_dir。

    模型调用异常原样上抛(ResilientModel 是唯一的瞬时重试 owner)。
    返回 StageOutcome:missing 非空 = 重试用尽仍缺的产物,由调用方决定是否判失败。
    """
    for name in deliverables:
        if not SAFE_NAME_RE.match(name) or name in PROGRAM_OWNED:
            raise ValueError(f"非法产物名: {name!r}")
    out = Path(out_dir)
    material_files = {} if getattr(agent.model, "read_materials", False) is True else None
    messages = [
        {"role": "system", "content": agent.instructions},
        {"role": "user", "content": build_user_message(task, docs, deliverables,
                                                      material_files=material_files)},
    ]
    model_kwargs = {"material_files": material_files} if material_files is not None else {}
    outcome = StageOutcome()
    lookup_rounds = 0
    pending = list(deliverables)
    # 总调用上限:每个 round 一次,加上最多 max_lookup_rounds 次补查
    for call_no in range(1, rounds + max_lookup_rounds + 1):
        reply = _call_model(agent.model, messages, **model_kwargs)
        outcome.rounds = call_no
        outcome.reply = reply
        got = parse_file_blocks(reply, pending)
        for name, body in got.items():
            _write_atomic(out / name, body)
            outcome.written.append(name)
            if on_write is not None:
                # 每个完整产物立即记账，避免后续追问耗尽额度时丢失已完成文件的来源。
                on_write(name)
        pending = [n for n in pending if n not in got]
        if not pending:
            break
        requests = LOOKUP_RE.findall(reply)
        if requests and lookup is not None and lookup_rounds < max_lookup_rounds:
            lookup_rounds += 1
            answers = []
            for key, query in requests[:max_lookups]:
                answers.append(f'<lookup_result key="{key.strip()}" query="{query.strip()[:80]}">\n'
                               f"{lookup(key.strip(), query.strip())}\n</lookup_result>")
            messages += [{"role": "assistant", "content": reply},
                         {"role": "user", "content":
                          "\n\n".join(answers) +
                          "\n\nUse these excerpts, then return the deliverables still missing: "
                          + ", ".join(pending) + "."}]
            continue
        if call_no - lookup_rounds >= rounds:
            break
        messages += [{"role": "assistant", "content": reply},
                     {"role": "user", "content":
                      "Your reply did not contain complete <file name=\"...\"> blocks for: "
                      + ", ".join(pending) + ". Resend ONLY those blocks, each with its full "
                      "content, in the required form."}]
    outcome.missing = pending
    return outcome


def say(owner: str, text: str) -> None:
    sys.stdout.write(f"[{owner}] {text}\n")
    sys.stdout.flush()
