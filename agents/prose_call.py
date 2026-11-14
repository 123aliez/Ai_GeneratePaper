"""正文单轮调用 —— 写正文的阶段不进 CodeAgent 循环。

为什么:CodeAgent 让模型把正文写成 `write_file(path, content="...")` 里的 Python
字符串字面量。普通字符串里 `\\b \\f \\a \\r \\t \\n \\v` 是转义符,于是 `\\beta` 落盘成
退格符+`eta`、`\\frac` 成换页符+`rac`、`\\ref` 成回车符+`ef`——方法章的公式整片损坏,
而 `\\cite` 恰好不受影响,引用检查发现不了。单轮调用里模型直接输出正文、Python 落盘,
这个问题从根上不存在。附带收益:idea.md / brief / plan 等由 Python 原文拼进提示词
(确定被读到,不靠模型自觉),也省掉 Agent 逐步读文件、每步重发历史的开销。

约定(与 structured_call 对齐):
- run_prose_call(model, agent_name, system, task, files, outputs) → bool
  单轮生成,按 `<<<FILE: 名字>>> … <<<END FILE>>>` 切出各产物,**全部必需产物齐全才
  落盘**(原子替换),否则一个都不写、返回 False,由调用方回落到 CodeAgent 路径。
- 模型调用异常向上抛(ResilientModel 是唯一重试 owner);调用方决定是否回落。
- 需要查文献的阶段(备料、证据挖掘)仍走 Agent:它们要 read_reference 的多轮读取。
"""
import os
import re
import sys
from pathlib import Path

FILE_BLOCK_RE = re.compile(
    r"<<<FILE:\s*(?P<name>[^>\n]+?)\s*>>>[ \t]*\r?\n(?P<body>.*?)\r?\n?<<<END FILE>>>", re.S)
_OPEN_RE = re.compile(r"<<<FILE:\s*[^>\n]+?\s*>>>")
# 模型有时把整块内容再包一层 ```markdown 围栏。
_OUTER_FENCE_RE = re.compile(r"\A\s*```[a-zA-Z]*[ \t]*\r?\n(.*?)\r?\n```\s*\Z", re.S)

# ── LaTeX 转义损坏检测(write_file 与单轮落盘共用) ─────────────────────────
# 正常正文里不会出现的控制字符:退格 \b、换页 \f、响铃 \a、垂直制表 \v、孤立回车 \r。
_HARD_CTRL_RE = re.compile(r"[\x07\x08\x0b\x0c]|\r(?!\n)")
# \t 与 \n 是合法空白,只能按"后面紧跟一个 LaTeX 命令的残余"来猜:\theta → TAB+heta。
_TAB_TEX_RE = re.compile(
    r"\t(?:heta|imes|ext(?:bf|it|tt|rm|sc)?\{|au(?![a-z])|ilde|frac|riangle|abular|extwidth)")
# 只收不会和正常行首撞车的长残余;`\nu`、`\neq` 的残余(行首 u / eq)太短,宁可漏报。
_NL_TEX_RE = re.compile(r"\n(?:abla|ewline|otin|onumber|oindent|ewcommand|ormalsize)")

ESCAPE_HELP = (
    "LaTeX backslashes were eaten by Python string escapes (e.g. \\beta became "
    "BACKSPACE+'eta', \\frac became FORMFEED+'rac', \\ref became CR+'ef', \\theta became "
    "TAB+'heta'). Pass the content as a RAW triple-quoted string — "
    "write_file(file_path=..., content=r\"\"\"...\"\"\") — or double every backslash, "
    "then call write_file again with the same full content.")


def latex_escape_damage(text: str) -> str:
    """文本里有没有"LaTeX 命令被字符串转义吃掉"的痕迹。返回首个证据的描述,无则空串。"""
    if not text:
        return ""
    for regex, label in ((_HARD_CTRL_RE, "control character"),
                         (_TAB_TEX_RE, "TAB followed by a LaTeX command remainder"),
                         (_NL_TEX_RE, "newline followed by a LaTeX command remainder")):
        m = regex.search(text)
        if m:
            ctx = text[max(0, m.start() - 20):m.end() + 20]
            return f"{label} near {ctx!r}"
    return ""


def _emit(owner: str, action: str, detail: str = "") -> None:
    # 与 run_agent_stage / structured_call 的行格式一致,进度过滤器照常解析。
    sys.stdout.write(f"[{owner:<8.8}] {action:<11} | {detail}\n")
    sys.stdout.flush()


def complete_text(model, messages) -> str:
    """一次完整生成的文本。优先走流式:整章正文一次生成要几分钟,网关对长响应不稳,
    ResilientModel 只在流式路径上做"断流丢弃 partial、快速重发"。没有流式接口才用 generate。"""
    stream = getattr(model, "generate_stream", None)
    if callable(stream):
        return "".join(getattr(delta, "content", "") or "" for delta in stream(messages))
    response = model.generate(messages)
    text = getattr(response, "content", "") or ""
    return text if isinstance(text, str) else str(text)


def single_turn_model(agent):
    """这个 Agent 能不能走单轮路径;能则返回它的 Model,否则 None。

    离线测试桩(_offline_test_stub)与没有 .model.generate 的对象一律走旧的 Agent 路径,
    全量离线套件因此不发任何真实请求。PROSE_SINGLE_TURN=false 可整体关掉(排障对照)。
    """
    if agent is None or getattr(type(agent), "_offline_test_stub", False) is True:
        return None
    if os.getenv("PROSE_SINGLE_TURN", "true").strip().lower() in ("0", "false", "no", "off"):
        return None
    model = getattr(agent, "model", None)
    return model if callable(getattr(model, "generate", None)) else None


def render_files(files: list[tuple[str, str]]) -> str:
    """[(标签, 原文)] → 提示词末尾的 PROVIDED FILES 块。原文逐字,不摘要不改写。"""
    blocks = []
    for label, text in files:
        blocks.append(f"=== BEGIN FILE: {label} ===\n{text.rstrip()}\n=== END FILE: {label} ===")
    return "\n\n".join(blocks)


def build_single_turn_prompt(task: str, files: list[tuple[str, str]],
                             outputs: list[str]) -> str:
    names = ", ".join(outputs)
    return (
        "SINGLE-TURN MODE. You have NO tools in this call: do not write Python and do not call "
        "read_file / write_file / read_reference. Every file the task below tells you to read is "
        "reproduced VERBATIM in the PROVIDED FILES block at the end — treat \"read X\" as \"use the "
        "provided copy of X\". The notes of the papers selected for this chapter are provided as "
        "`reference note: <key>`; a paper whose note is not provided may be cited only for what "
        "its one-line summary in the reference index says. If something the task needs is not "
        "provided, mark the gap ([MISSING INFO] / [CITATION NEEDED]) instead of guessing.\n\n"
        "OUTPUT FORMAT (strict). Where the task says to write a file, output its complete content "
        "between markers, each marker on its own line:\n"
        "<<<FILE: name>>>\n...full content...\n<<<END FILE>>>\n"
        f"Required files, in this order: {names}. Output nothing outside the markers — no "
        "preamble, no commentary, no code fences around a file. Write every LaTeX command with "
        "single backslashes exactly as it must appear in the file (`\\cite{key}`, `\\beta`).\n"
        "Other files the task or your standing instructions mention (todo.md, decision.md, ...) "
        "are NOT written in this call unless they are in the required list above.\n\n"
        "=== TASK ===\n"
        f"{task.rstrip()}\n\n"
        "=== PROVIDED FILES ===\n"
        f"{render_files(files)}\n"
    )


def parse_file_blocks(text: str) -> tuple[dict[str, str], bool]:
    """从模型输出切出 {文件名: 内容}。第二个返回值:是否疑似被截断
    (开标记比完整块多 = 最后一块没写完,多半是撞了 max_tokens)。"""
    blocks: dict[str, str] = {}
    for m in FILE_BLOCK_RE.finditer(text or ""):
        name = os.path.basename(m.group("name").strip().strip("'\"`"))
        body = m.group("body")
        fenced = _OUTER_FENCE_RE.match(body)
        if fenced:
            body = fenced.group(1)
        blocks.setdefault(name, body.strip("\n") + "\n")
    truncated = len(_OPEN_RE.findall(text or "")) > len(FILE_BLOCK_RE.findall(text or ""))
    return blocks, truncated


def run_prose_call(model, agent_name: str, system: str, task: str,
                   files: list[tuple[str, str]], outputs: dict[str, str]) -> bool:
    """单轮生成一组正文产物。outputs = {文件名: 落盘路径},全部必需。

    任何一项缺失/为空/疑似截断/带转义损坏 → 不落盘任何文件,返回 False。
    """
    from smolagents.models import ChatMessage, MessageRole

    names = list(outputs)
    user = build_single_turn_prompt(task, files, names)
    _emit(agent_name, "single-turn", f"prose call → {', '.join(names)} "
                                     f"({len(user) // 1000}k chars in)")
    text = complete_text(model, [
        ChatMessage(role=MessageRole.SYSTEM, content=system),
        ChatMessage(role=MessageRole.USER, content=user),
    ])
    blocks, truncated = parse_file_blocks(text)
    problem = ""
    if truncated:
        problem = ("output looks truncated (an opened file block never closed) — "
                   "raise the model's max output tokens (ANTHROPIC_MAX_TOKENS)")
    else:
        for name in names:
            body = blocks.get(name, "")
            if not body.strip():
                problem = f"no content for {name}"
                break
            damage = latex_escape_damage(body)
            if damage:
                problem = f"{name}: {damage}"
                break
    if problem:
        _emit(agent_name, "single-fail", f"{problem}; falling back to the agent path")
        return False
    for name in names:
        path = Path(outputs[name])
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        tmp.write_text(blocks[name], encoding="utf-8")
        os.replace(tmp, path)
        _emit(agent_name, "write_file", str(path).replace("\\", "/"))
    return True
