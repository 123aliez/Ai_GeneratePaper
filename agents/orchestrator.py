import contextlib
import io
import json
import os
import re
import shutil
import sys
from pathlib import Path
from .tools import set_agent_context

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from config import (
    MAX_REVISION_ROUNDS,
    REVIEW_SCORE_THRESHOLD,
    REFERENCE_INDEX_PATH as _REFERENCE_INDEX_PATH,
    PAPER_MODE,
    DATA_ROOT,
    IDEA_PATH,
    DATA_INDEX_PATH,
)
from .chapter_type import (
    resolve_run_route, route_banner,
    IDEA, DATA, MIXED, BLOCKING, ADVISORY, OFF,
)
# 写作路由的常量与函数模块级导入。原先是函数体内懒加载 + 宽泛 except:outline.md
# 一旦读取或解析出错,mode_clause 仍是空串——等于"没有任何写作契约"。现在解析失败
# 直接硬停(见 run_4stage_with_progress 里的 OutlineRouteError 分支)。
from .outline import (
    FULL, CROSS_CHAPTER_STATE, XCHAP_HEADINGS,
    OutlineRouteError,
    build_outline_excerpt, build_mode_clause, build_mode_review_clause,
    resolve_write_mode, read_brief_fingerprint, chapter_fingerprint,
)

# Project-local reference index (config points it at this project's references/,
# not the survey's). Kept as a module global for build_reference_excerpt.
REFERENCE_INDEX_PATH = Path(_REFERENCE_INDEX_PATH)


PROGRESS_PATTERNS = [
    (re.compile(r"Stage\s+1|draft-v1|Stage 1", re.I), "[Manager] Stage 1/4 | Drafting draft-v1.md"),
    (re.compile(r"Stage\s+2|review-v1|Stage 2", re.I), "[Manager] Stage 2/4 | Reviewing draft-v1.md"),
    (re.compile(r"Stage\s+3|draft-v2|Stage 3", re.I), "[Manager] Stage 3/4 | Revising draft-v2.md"),
    (re.compile(r"Stage\s+4|final\.md|Stage 4", re.I), "[Manager] Stage 4/4 | Finalizing final.md and final.zh.md"),
    (re.compile(r"draft_agent", re.I), "[Manager] Agent     | Calling draft_agent"),
    (re.compile(r"review_agent", re.I), "[Manager] Agent     | Calling review_agent"),
]

TOOL_CALL_RE = re.compile(r"\b(read_file|write_file|list_folder)\s*\([^)]*(?:file_path|folder_path)\s*=\s*['\"]([^'\"]+)['\"]", re.S)
ERROR_RE = re.compile(r"\b(error|exception|failed|traceback|abort|warning)\b", re.I)
NOTICE_ERROR_RE = re.compile(r"Error in generating model output|litellm\.|AnthropicException|OpenAIException|BadGatewayError|MidStreamFallbackError|InternalServerError|ConnectError|Timeout|Server disconnected", re.I)
NOTICE_IGNORE_RE = re.compile(r"LiteLLM\.Info:\s*If you need to debug this error", re.I)
RAW_NOISE_RE = re.compile(r"^\s*(Thought:|<code>|Code execution failed at line|import\s|from\s|def\s|for\s|while\s|if\s|elif\s|else:|return\s|[A-Za-z_][A-Za-z0-9_]*\s*=)", re.I)
STRUCTURED_PROGRESS_RE = re.compile(r"(?:\[(?:Manager|Draft|Review)\]\s*)?(?:\[(?:Stage|Agent|Tool|Verify|Notice|Done|Error)\]|(?:Stage|Agent|Tool|Verify|Notice|Done|Error)\s*\|)[^\n\r]*")
LEGACY_PROGRESS_RE = re.compile(r"^\[(Manager|Draft|Review)\]\[(Stage|Agent|Tool|Verify|Notice|Done|Error)\]\s*(.*)$")
ABS_PATH_RE = re.compile(r"[A-Za-z]:[/\\][^\s'\"),]+")
TRANSIENT_ERROR_RE = re.compile(r"SSL:|UNEXPECTED_EOF|Connection error|ConnectError|Server disconnected|Timeout|timed out|InternalServerError", re.I)

REVIEW_JSON_SCHEMA_HINT = """{
  "scores": {"accuracy": 1-5, "completeness": 1-5, "clarity": 1-5, "structure": 1-5, "readability": 1-5, "ai_traces": 1-5, "style_consistency": 1-5, "overall": 1-5},
  "decision": "REVISE" or "ACCEPT",
  "must_fix": [{"id": "MF1", "location": "section/paragraph", "issue": "...", "suggestion": "..."}],
  "should_fix": [{"id": "SF1", "location": "...", "issue": "...", "suggestion": "..."}],
  "consider": [{"id": "C1", "note": "..."}],
  "needs_citation": [{"sentence": "the exact sentence needing a citation", "suggested_key": "REF-0003 or null"}]
}"""

VERIFY_JSON_SCHEMA_HINT = """{
  "all_resolved": true or false,
  "items": [{"id": "MF1", "resolved": true or false, "note": "how it was resolved, or what is still missing"}]
}"""


def display_path(path: str) -> str:
    normalized = path.replace("\\", "/")
    marker = "/paper/"
    if marker in normalized:
        return "paper/" + normalized.split(marker, 1)[1]
    marker = "/survey/"
    if marker in normalized:
        return normalized.split(marker, 1)[1]
    if ":/" in normalized:
        return Path(normalized).name
    return normalized


def sanitize_progress_text(text: str) -> str:
    return ABS_PATH_RE.sub(lambda match: display_path(match.group(0)), text)


def format_progress_message(message: str) -> str:
    message = sanitize_progress_text(" ".join(message.split()))
    legacy = LEGACY_PROGRESS_RE.match(message)
    if legacy:
        owner, kind, detail = legacy.groups()
        detail = detail.strip()
        if kind == "Tool":
            tool_match = re.match(r"(\w+)\s*:\s*(.*)", detail)
            if tool_match:
                return f"[{owner}] Tool      | {tool_match.group(1)} -> {tool_match.group(2)}"
        return f"[{owner}] {kind:<9} | {detail}"
    return message


def print_stream_progress(content: str, seen: set[str], owner: str = "Manager", output=None, include_stage_hints: bool = False) -> None:
    if not content:
        return
    output = output or None

    def emit(message: str) -> None:
        if output is None:
            print(message, flush=True)
        else:
            output.write(message + "\n")
            output.flush()

    text = " ".join(content.split())

    if include_stage_hints:
        for pattern, message in PROGRESS_PATTERNS:
            if pattern.search(content) and message not in seen:
                emit(message)
                seen.add(message)

    for tool_name, path in TOOL_CALL_RE.findall(content):
        message = f"[{owner:<8.8}] {tool_name:<10} | {display_path(path)}"
        if message not in seen:
            emit(message)
            seen.add(message)

    if RAW_NOISE_RE.search(text):
        return

    for raw_message in STRUCTURED_PROGRESS_RE.findall(content):
        message = format_progress_message(raw_message)
        if "Thought:" in message or "Code execution failed" in message or "<code>" in message:
            continue
        if message not in seen:
            emit(message)
            seen.add(message)
    if NOTICE_IGNORE_RE.search(text):
        return
    if NOTICE_ERROR_RE.search(content):
        if text and text not in seen:
            emit(f"[{owner:<8.8}] notice     | {sanitize_progress_text(text[:220])}")
            seen.add(text)


class ProgressFilteringStdout(io.StringIO):
    """拦截 Agent 的 stdout 流式输出,重打成进度行。

    heartbeat=True 时(standalone 调用,如 --expand),每隔 HEARTBEAT_INTERVAL 个
    字符打印一行"thinking 心跳"(用 \\r 覆盖),让用户看到 Agent 还在推理而不是卡住。
    流水线内(有 Stage 表格)传 False,避免干扰。
    """

    HEARTBEAT_INTERVAL = 800   # 每接收这么多字符打一次心跳

    def __init__(self, owner: str, seen: set[str], output, heartbeat: bool = False):
        super().__init__()
        self.owner = owner
        self.seen = seen
        self.output = output
        self.buffer = ""
        self.heartbeat = heartbeat
        self._received = 0
        self._last_heartbeat = 0

    def write(self, text):
        self.buffer += str(text)
        if self.heartbeat:
            self._received += len(str(text))
            if self._received - self._last_heartbeat >= self.HEARTBEAT_INTERVAL:
                self._last_heartbeat = self._received
                line = (f"[{self.owner:<8.8}] thinking    | "
                        f"已接收 {self._received} 字推理内容,仍在进行...")
                # 心跳走 self.output(测试可捕获;正式运行时 = 真实 stdout),用 \r 覆盖
                if self.output is not None:
                    self.output.write("\r" + line)
                    self.output.flush()
                else:
                    print("\r" + line, end="", flush=True)
        print_stream_progress(str(text), self.seen, self.owner, self.output)
        return len(str(text))

    def flush(self):
        return None


def _is_permanent_failure(exc: BaseException) -> bool:
    """异常（沿 __cause__/__context__ 链）是否源于一个永久错误。

    ResilientModel 对 401/403/413/422 这类永久错误原样上抛（不重试、不包装），
    但 CodeAgent 会把它包成 ``AgentGenerationError``，从而丢失"这是永久错误"的语义。
    若不在这里识别，``run_agent_stage`` 会对永久错误重跑 4 次 CodeAgent.run()，每次
    撞同一个 401，白白烧 token。沿异常链找根因的 status_code，落在永久集合就立即失败。
    """
    from .retry_policy import PERMANENT_STATUS
    seen = set()
    cur: BaseException | None = exc
    depth = 0
    while cur is not None and depth < 10:  # 限深，防自引用循环
        if id(cur) in seen:
            break
        seen.add(id(cur))
        status = getattr(cur, "status_code", None)
        if isinstance(status, int) and status in PERMANENT_STATUS:
            return True
        cur = cur.__cause__ or cur.__context__
        depth += 1
    return False


def _safe_exc_label(exc: BaseException) -> str:
    """给终端日志/返回值用的安全异常标签：只取类名 + status_code，不取异常文本。

    异常文本（str(exc)）可能含网关回显的 body、prompt 片段或 api_key——CodeAgent
    把 SDK 异常包成 AgentGenerationError 时会带上原文。直接拼进日志/返回值有泄漏风险。
    这里沿异常链找一个 status_code，输出 ``TransientModelUnavailable(status=503)`` 这种
    固定结构；链里没有 status_code 就只给类名。
    """
    name = type(exc).__name__
    # 沿链找 status_code（与 _is_permanent_failure 同样的遍历）
    seen = set()
    cur: BaseException | None = exc
    depth = 0
    while cur is not None and depth < 10:
        if id(cur) in seen:
            break
        seen.add(id(cur))
        status = getattr(cur, "status_code", None)
        if isinstance(status, int):
            return f"{name}(status={status})"
        cur = cur.__cause__ or cur.__context__
        depth += 1
    return name


def chapter_name_from_folder(folder_path: str) -> str:
    return Path(folder_path.replace("\\", "/")).name


# ── 进度汇总表 ─────────────────────────────────────────────────────────
# 把 `results` dict + 工作区实际产物汇总成一张 Stage 状态表。`--progress` 每章跑完
# 打印一张(见 run_4stage_with_progress 末尾),`--all` 每章结束时也打印一张(见
# run.py::run_all_chapters)。数据源是确定性的:results 里的 "skipped"/error dict +
# 关键产物的文件存在性,不依赖 Agent 输出格式。
#
# 状态枚举:done(产物齐全)/ skipped(断点续跑跳过)/ running(当前执行中)/
#           failed(路由硬停或 agent 调用失败)/ pending(未执行到)。
# 判断失败靠外层(results 无 stageX 字段且 final 不存在 = 提前 return)。

# 每个 Stage → (results 字段, 关键产物文件)。stage5 用布尔 stage5_xchap_ok。
_STAGE_ARTIFACTS = [
    ("0 证据挖掘",   "stage0_evidence", ["evidence-pack.md"]),
    ("1a 规划",      "stage1_plan",     ["draft-v1.plan.md"]),
    ("1b 起草",      "stage1_parts",    ["draft-v1.md"]),
    ("2 评审",       "stage2",          ["review-v1.json", "review-v1.md"]),
    ("3 收敛修订",   "stage3",          ["draft-v2.md"]),
    ("4 定稿",       "stage4",          ["final.md", "final.zh.md"]),
    ("5 跨章交接",   "stage5",          ["cross-chapter-state.md"]),
]


def render_stage_table(folder_path: str, results: dict,
                       running_stage: int = -1) -> str:
    """渲染一章的 Stage 进度表。running_stage 指正在执行的 Stage 序号(-1=全完成)。

    results 是 run_4stage_with_progress 的返回 dict;folder_path 用于检查关键产物
    文件是否存在(决定 done/skipped)。不抛错:任何字段缺失按 pending 处理。
    """
    rows = []
    for index, (label, field, artifacts) in enumerate(_STAGE_ARTIFACTS):
        # 状态判定
        if running_stage == index:
            status = "进行中"
        elif results.get("route_blocked") and index >= _blocked_stage_index(results):
            status = "未执行"
        elif field == "stage5":
            status = ("完成" if results.get("stage5_xchap_ok") is True
                      else "未执行" if running_stage < index else "失败")
        else:
            val = results.get(field)
            if isinstance(val, dict) and "error" in val:
                status = "失败"
            elif val == "skipped":
                status = "跳过"
            elif _any_artifact_exists(folder_path, artifacts):
                status = "完成"
            elif running_stage < index:
                status = "未执行"
            else:
                status = "失败"

        present = [a for a in artifacts if os.path.exists(
            os.path.join(folder_path, a))]
        artifact_str = ", ".join(present) if present else "-"
        rows.append((label, status, artifact_str))

    width_label = max(len(r[0]) for r in rows)
    width_status = max(len(r[1]) for r in rows)
    width_art = max(len(r[2]) for r in rows)

    lines = [
        f"| {'阶段':<{width_label}} | {'状态':<{width_status}} | {'产物':<{width_art}} |",
        f"|{'-' * (width_label + 2)}|{'-' * (width_status + 2)}|{'-' * (width_art + 2)}|",
    ]
    for label, status, artifact in rows:
        lines.append(f"| {label:<{width_label}} | {status:<{width_status}} | "
                     f"{artifact:<{width_art}} |")
    return "\n".join(lines)


def _any_artifact_exists(folder_path: str, artifacts: list[str]) -> bool:
    return any(os.path.exists(os.path.join(folder_path, a)) for a in artifacts)


def _blocked_stage_index(results: dict) -> int:
    """路由硬停时,报告从哪个 Stage 起未执行(粗略:到首个被 blocked 的阶段)。"""
    if results.get("route_blocked"):
        for index, (label, field, _artifacts) in enumerate(_STAGE_ARTIFACTS):
            if field == "stage0_evidence" and "stage0" in str(results.get("route_blocked")):
                return index
        # 默认:阻塞发生在 Stage 0(证据挖掘前)
        return 0
    return len(_STAGE_ARTIFACTS)


def _print_stage_table(folder_path: str, results: dict,
                       running_stage: int = -1) -> None:
    """打印一章的 Stage 进度表。表格整体打印,不覆盖流式输出。"""
    print("\n=== 章节进度 ===", flush=True)
    print(render_stage_table(folder_path, results, running_stage), flush=True)
    print("=================", flush=True)


def _finish_with_table(folder_path: str, results: dict) -> dict:
    """run_4stage_with_progress 的统一返回出口:打印进度表后返回 results。

    所有提前 return(路由硬停 / verify 失败 / agent 调用失败)都走这里,确保无论
    成功还是失败,调用方都能看到一章的 Stage 状态表,而不是只有一列列的日志。
    """
    try:
        _print_stage_table(folder_path, results)
    except Exception as exc:
        print(f"[Manager  ] notice     | 进度表格渲染失败(不影响结果): {str(exc)[:120]}", flush=True)
    return results


def build_reference_excerpt(chapter: str) -> str:
    if not REFERENCE_INDEX_PATH.exists():
        return (
            f"# Reference index excerpt for {chapter}\n"
            f"(No reference index at {REFERENCE_INDEX_PATH.name}. This project "
            f"cites from references/bibliography.md instead; work notes-free.)\n"
            f"Use search_references(query, chapter) only if an index exists.\n"
        )
    lines = REFERENCE_INDEX_PATH.read_text(encoding="utf-8").splitlines()
    rows = []
    total = 0
    for line in lines:
        if not line.startswith("| REF-"):
            continue
        total += 1
        columns = [column.strip() for column in line.strip("|").split("|")]
        if len(columns) < 10:
            continue
        if columns[4] == chapter or chapter in columns[5]:
            rows.append(line)
    header = [
        f"# Reference index excerpt for {chapter}",
        f"Filter: Primary Chapter == \"{chapter}\" OR Also Relevant To contains \"{chapter}\"",
        f"Rows: {len(rows)} of {total}",
        "",
        "Evidence grades: A=peer-reviewed, B=arXiv/preprint, C=technical report/system card, D=blog/model card, E=third-party/weak source.",
        "Do not read references/index.md directly; use this excerpt or bounded search_references for additional lookup.",
        "",
        "| ID | Title | Year | Evidence | Primary Chapter | Also Relevant To | Paradigm Tags | Key Use | Note Path | Full Text Path |",
        "|---|---|---:|:---:|---|---|---|---|---|---|",
    ]
    return "\n".join(header + rows) + "\n"


def parse_brief_sections(folder_path: str) -> list[dict]:
    brief_path = Path(folder_path) / "brief.md"
    if not brief_path.exists():
        return []
    sections = []
    current = None
    section_re = re.compile(r"^\s*(\d+)\.\s+\*\*(.+?)\*\*\s+\(~?(\d+)\s+words?\)", re.I)
    for line in brief_path.read_text(encoding="utf-8").splitlines():
        match = section_re.match(line)
        if match:
            current = {
                "number": int(match.group(1)),
                "title": match.group(2).strip(),
                "target_words": int(match.group(3)),
                "bullets": [],
            }
            sections.append(current)
        elif current and line.strip().startswith("-"):
            current["bullets"].append(line.strip())
    return sections


def build_stage1_parts(sections: list[dict]) -> list[dict]:
    """把小节分成起草段。段数随小节数自适应,最多三段。

    分组只依据小节数量,不做任何按标题的特例判断。此前从 survey 引擎继承来一个
    Alignment 章特例(含 "rlvr" + "constitutional" 小节时改用异常分组),对实验型
    论文没有意义,只会在小节命名恰好命中时把边界打乱,已删除。

    段数不再固定为三:Abstract 这类章只有 1-2 个小节,硬凑三段会生出
    "Chapter part 2/3" 这种没有来源的空段,并给它们 700 词的目标——150 词的摘要
    因此被要求写成两千词。段数 = min(小节数, 3),没有小节时退回单段。
    """
    count = len(sections)
    if count >= 6:
        groups = [sections[:2], sections[2:4], sections[4:]]
    elif count >= 3:
        size = (count + 2) // 3
        groups = [sections[:size], sections[size:size * 2], sections[size * 2:]]
    elif count:
        groups = [[section] for section in sections]   # 1 或 2 个小节 → 1 或 2 段
    else:
        groups = [[]]  # brief 没解析出小节,单段兜底(调用方会警告)

    parts = []
    for index, group in enumerate(groups, start=1):
        titles = [section["title"] for section in group]
        words = sum(section["target_words"] for section in group)
        numbers = [section["number"] for section in group]
        if not titles:
            titles = [f"Chapter part {index}"]
            words = 700
        parts.append({
            "index": index,
            "output": f"draft-v1.part-{index}.md",
            "titles": titles,
            "numbers": numbers,
            "target_words": words,
        })
    return parts


def part_family(part: dict, route: dict) -> str:
    """Resolve the evidence family for one Stage-1 part.

    A whole-paper brief splits across families — part 1 might be Abstract +
    Introduction (idea) while part 3 is Results + Conclusion (data). Giving all
    three parts the chapter-level rule is what would let the Method part be
    drafted as a results recap, so each part is routed on the sections it
    actually covers. Falls back to the chapter route when the part covers no
    typed section.
    """
    from .chapter_type import route_for_type
    section_types = route.get("section_types") or {}
    families = {route_for_type(section_types[n])[0]
                for n in part.get("numbers", []) if n in section_types}
    if not families:
        return route.get("family", MIXED)
    if len(families) == 1:
        return families.pop()
    return MIXED


def format_stage1_parts(parts: list[dict]) -> str:
    """把段边界渲染成规划提示词里的一段说明。

    字数区间按比例放宽,不用固定的 ±词数。原先是 `max(300, target-100)` 到
    `target+150`:那个 300 下限是照 survey 章节(每段 700+ 词)定的,用在一个 150
    词的摘要上会被抬成 "300-300 words" —— 目标翻倍,区间还塌成一个点。
    """
    blocks = []
    for part in parts:
        target = max(1, part["target_words"])
        lower = max(60, int(target * 0.85))
        upper = max(lower + 40, int(target * 1.2) + 30)
        blocks.append(
            f"Part {part['index']} -> {part['output']}\n"
            f"Target length: {lower}-{upper} words\n"
            f"Covers: " + "; ".join(part["titles"])
        )
    return "\n\n".join(blocks)


def concatenate_stage1_parts(folder_path: str, part_count: int = 3) -> None:
    """把本次实际产出的 part 拼成 draft-v1.md。

    part_count 必须传本次运行的段数:段数自适应之后,上一次跑三段留下的
    draft-v1.part-3.md 若被无条件拼进来,这一次两段的草稿会多出一截旧内容。
    """
    folder = Path(folder_path)
    chunks = []
    for index in range(1, max(1, part_count) + 1):
        part_path = folder / f"draft-v1.part-{index}.md"
        if part_path.exists():
            chunks.append(part_path.read_text(encoding="utf-8").strip())
    (folder / "draft-v1.md").write_text("\n\n".join(chunk for chunk in chunks if chunk) + "\n", encoding="utf-8")


def tail_text(path: str, max_chars: int) -> str:
    """Return the trailing max_chars of a file (used to pass the prior part's
    ending into the next part's draft prompt). Empty string if absent."""
    if not os.path.exists(path):
        return ""
    text = Path(path).read_text(encoding="utf-8", errors="replace").strip()
    if len(text) <= max_chars:
        return text
    return "…" + text[-max_chars:]


def extract_json_block(text: str):
    """Parse a JSON object out of possibly-noisy text.

    Tries a ```json fenced block first, then the outermost {...}. Returns the
    parsed object or None. Adapted from AI-Scientist's extract_json_between_markers.
    """
    if not text:
        return None
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S)
    candidates = []
    if fenced:
        candidates.append(fenced.group(1))
    first, last = text.find("{"), text.rfind("}")
    if first != -1 and last != -1 and last > first:
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


def read_json_artifact(folder_path: str, name: str):
    """Read and parse a JSON artifact written by an agent. Returns None if the
    file is missing or unparseable (caller decides how to degrade)."""
    path = os.path.join(folder_path, name)
    if not os.path.exists(path):
        return None
    return extract_json_block(Path(path).read_text(encoding="utf-8", errors="replace"))


def _promote_draft(folder_path: str, round_draft: str) -> None:
    """Copy the accepted round's draft to draft-v2.md (the canonical Stage 3 output)."""
    src = os.path.join(folder_path, round_draft)
    if os.path.exists(src):
        content = Path(src).read_text(encoding="utf-8", errors="replace")
        Path(os.path.join(folder_path, "draft-v2.md")).write_text(content, encoding="utf-8")
        print(f"[Manager  ] write_file | draft-v2.md (from {round_draft})", flush=True)


def _unresolved_items(verdict) -> list:
    """Extract the still-unresolved checklist items from a verify verdict."""
    if not isinstance(verdict, dict):
        return []
    items = verdict.get("items", [])
    if not isinstance(items, list):
        return []
    return [it for it in items if isinstance(it, dict) and not it.get("resolved")]


def _checklist_verdict(frozen: list, verdict) -> tuple:
    """Strictly verify a VERIFY verdict against the frozen acceptance checklist.

    Returns (all_resolved, missing, unknown, dupes):
      - all_resolved: True only if every frozen id appears exactly once, resolved.
      - missing: frozen ids the verdict did not report at all.
      - unknown: ids the verdict reported that are not in the frozen checklist.
      - dupes: ids reported more than once.

    This makes acceptance immune to a hollow ``{"all_resolved": true, "items": []}``:
    acceptance is derived from the checklist, not trusted from a top-level boolean.
    """
    frozen_ids = [it.get("id") for it in frozen if isinstance(it, dict)]
    frozen_set = set(frozen_ids)
    if not isinstance(verdict, dict):
        return False, frozen_ids, [], []
    items = verdict.get("items", [])
    if not isinstance(items, list):
        return False, frozen_ids, [], []
    reported_ids = [it.get("id") for it in items if isinstance(it, dict)]
    reported_set = set(reported_ids)
    missing = [fid for fid in frozen_ids if fid not in reported_set]
    unknown = [rid for rid in reported_set if rid not in frozen_set]
    dupes = [rid for rid in reported_set if reported_ids.count(rid) > 1]
    # Resolved iff every frozen id is present exactly once and marked resolved.
    unresolved_reported = [it for it in items
                           if isinstance(it, dict) and not it.get("resolved")]
    all_resolved = (not missing and not unknown and not dupes and not unresolved_reported)
    return all_resolved, missing, unknown, dupes


def _escalate_unresolved(folder_path: str, rounds: int) -> None:
    """At the round cap, append any unresolved checklist items to todo.md and
    decision.md so they surface for human attention instead of looping forever."""
    verdict = read_json_artifact(folder_path, f"review-verify.round-{rounds}.json")
    unresolved = _unresolved_items(verdict)
    if not unresolved:
        return
    lines = [f"\n## Unresolved after {rounds} revision rounds (needs human attention)"]
    for it in unresolved:
        lines.append(f"- [{it.get('id', '?')}] {it.get('location', '')}: {it.get('note') or it.get('issue', '')}")
    block = "\n".join(lines) + "\n"
    for fname in ("todo.md", "decision.md"):
        path = Path(os.path.join(folder_path, fname))
        existing = path.read_text(encoding="utf-8", errors="replace") if path.exists() else ""
        path.write_text(existing + block, encoding="utf-8")


def run_convergence_loop(draft_agent, review_agent, folder_path, folder_rel,
                         reference_excerpt, structure,
                         run_agent_stage, verify, mode_clause: str = "",
                         family: str = MIXED):
    """Freeze the first review's MUST FIX list as an acceptance checklist, then
    iterate revise -> verify up to MAX_REVISION_ROUNDS. Each verify round re-checks
    ONLY the frozen checklist (no new issues). Stop when every frozen item is
    resolved; at the round cap, promote the last round and escalate leftovers.

    run_agent_stage and verify are passed in from run_4stage_with_progress (they
    carry that call's closure state). Every round's artifacts are round-numbered so
    a crashed run resumes instead of restarting. Returns the last draft result.

    mode_clause 是整篇写作契约。修订轮同样要带上它:收敛循环会重写整篇草稿,
    不带这段的话,某一段在第 2 轮可能被改得不遵守跨章约定。"""
    review = read_json_artifact(folder_path, "review-v1.json")
    frozen = review.get("must_fix", []) if isinstance(review, dict) else None

    # Fallback: unparseable review or missing must_fix → single revise pass (old behaviour).
    if not isinstance(frozen, list):
        print("[Manager  ] notice     | review-v1.json unparseable; single revise pass", flush=True)
        set_agent_context("Draft")
        result = run_agent_stage(draft_agent, "Draft", (
            f"{idea_clause(family) if PAPER_MODE == 'experiment' else ''}"
            f"Read brief.md, input.md, draft-v1.md, review-v1.md from '{folder_path}'. "
            f"Also read '{structure}'. Do not read references/index.md directly; use the filtered reference excerpt below. "
            f"{mode_clause}"
            f"Address ALL 'MUST FIX' items. Write '{folder_path}/draft-v2.md' and '{folder_path}/todo.md'. "
            f"If major rewrite, also write '{folder_path}/decision.md'.\n\n{reference_excerpt}"
        ))
        set_agent_context("Manager")
        return result

    if not frozen:
        print("[Manager  ] converge   | no MUST FIX items; one polish pass for SHOULD FIX", flush=True)
        set_agent_context("Draft")
        result = run_agent_stage(draft_agent, "Draft", (
            f"{idea_clause(family) if PAPER_MODE == 'experiment' else ''}"
            f"Read brief.md, input.md, draft-v1.md, review-v1.md from '{folder_path}'. "
            f"Also read '{structure}'. Do not read references/index.md directly; use the filtered reference excerpt below. "
            f"{mode_clause}"
            f"There are no MUST FIX items. Address SHOULD FIX items where supported by evidence, "
            f"then write a clean '{folder_path}/draft-v2.md' and '{folder_path}/todo.md'.\n\n{reference_excerpt}"
        ))
        set_agent_context("Manager")
        return result

    print(f"[Manager  ] converge   | frozen acceptance checklist: {len(frozen)} MUST FIX item(s)", flush=True)
    checklist_text = json.dumps(frozen, ensure_ascii=False, indent=2)
    last_result = None

    for round_no in range(1, MAX_REVISION_ROUNDS + 1):
        draft_round = f"draft-v2.round-{round_no}.md"
        verdict_round = f"review-verify.round-{round_no}.json"
        prev_draft = "draft-v1.md" if round_no == 1 else f"draft-v2.round-{round_no - 1}.md"

        # -- revise against the frozen checklist --
        print(f"\n[Manager  ] Round {round_no}/{MAX_REVISION_ROUNDS} | revise → {folder_rel}/{draft_round}", flush=True)
        if not os.path.exists(os.path.join(folder_path, draft_round)):
            set_agent_context("Draft")
            last_result = run_agent_stage(draft_agent, "Draft", (
                f"{idea_clause(family) if PAPER_MODE == 'experiment' else ''}"
                f"Read brief.md, input.md, {prev_draft}, review-v1.md from '{folder_path}'. "
                f"Also read '{structure}'. Do not read references/index.md directly; use the filtered reference excerpt below.\n\n"
                f"{mode_clause}"
                f"Resolve EVERY item in this frozen acceptance checklist (JSON):\n{checklist_text}\n\n"
                f"Make the concrete change at each item's location. Preserve everything already correct — "
                f"do not rewrite unaffected paragraphs. Write the full revised draft to '{folder_path}/{draft_round}'.\n\n"
                f"{reference_excerpt}"
            ))
            set_agent_context("Manager")
            if not verify([draft_round]):
                return last_result

        # -- verify ONLY the frozen checklist --
        print(f"[Manager  ] Round {round_no}/{MAX_REVISION_ROUNDS} | verify → {folder_rel}/{verdict_round}", flush=True)
        if not os.path.exists(os.path.join(folder_path, verdict_round)):
            set_agent_context("Review")
            run_agent_stage(review_agent, "Review", (
                f"Read {draft_round} and brief.md from '{folder_path}'. Do not read references/index.md directly; use the filtered reference excerpt below.\n\n"
                f"VERIFY mode: check ONLY whether each item in this frozen checklist is now resolved in {draft_round}. "
                f"Do NOT raise new issues.\n{checklist_text}\n\n"
                f"Write '{folder_path}/{verdict_round}' with EXACTLY this schema (valid JSON only):\n"
                f"{VERIFY_JSON_SCHEMA_HINT}\n"
                f"Set 'all_resolved' true only if every checklist item is resolved.\n\n"
                f"{reference_excerpt}"
            ))
            set_agent_context("Manager")
            if not verify([verdict_round]):
                return last_result

        verdict = read_json_artifact(folder_path, verdict_round)
        all_resolved, missing, unknown, dupes = _checklist_verdict(frozen, verdict)

        if all_resolved:
            print(f"[Manager  ] converge   | all {len(frozen)} item(s) resolved in round {round_no}", flush=True)
            _promote_draft(folder_path, draft_round)
            return last_result

        unresolved = _unresolved_items(verdict)
        n_open = len(unresolved)
        if missing:
            n_open = max(n_open, len(missing))
            print(f"[Manager  ] converge   | round {round_no}: {len(missing)} checklist item(s) not verified ({', '.join(str(x) for x in missing[:5])})", flush=True)
        if unknown:
            print(f"[Manager  ] converge   | round {round_no}: verdict reported unknown id(s): {', '.join(str(x) for x in unknown[:5])}", flush=True)
        if dupes:
            print(f"[Manager  ] converge   | round {round_no}: verdict duplicated id(s): {', '.join(str(x) for x in dupes[:5])}", flush=True)
        print(f"[Manager  ] converge   | round {round_no}: {n_open or '?'} item(s) still unresolved", flush=True)

    # Round cap reached with items still open → promote last round, escalate leftovers.
    _promote_draft(folder_path, f"draft-v2.round-{MAX_REVISION_ROUNDS}.md")
    _escalate_unresolved(folder_path, MAX_REVISION_ROUNDS)
    print("[Manager  ] converge   | round cap reached; unresolved items escalated to todo.md/decision.md", flush=True)
    return last_result


ROUTING_CLAUSES = {
    IDEA: (
        "CHAPTER TYPE: {type} — an IDEA chapter. Its subject is the contribution "
        "itself: what is new, why it works, how it is built. Write it from idea.md "
        "(read in full), which is the author's own statement of the novelty. The "
        "data-index / data/ results are only supporting evidence — quote at most a "
        "headline number where it motivates the idea, and leave detailed reporting "
        "to the results chapter. Never substitute experimental outcomes for an "
        "explanation of the mechanism, and never invent a contribution the author "
        "did not claim: write [IDEA NEEDED] or [DESIGN DETAIL NEEDED] instead."
    ),
    DATA: (
        "CHAPTER TYPE: {type} — a DATA chapter. Its subject is what the experiments "
        "showed. Every number must appear verbatim in data-index.md (the three-level "
        "index into data/); write [MISSING DATA] for anything absent. Read idea.md "
        "only to decide which comparisons matter and how to narrate them — do not "
        "re-explain the method design here, it belongs to the method chapter."
    ),
    MIXED: (
        "CHAPTER TYPE: {type} — a MIXED chapter: it must connect the contribution "
        "to the evidence. Draw the claim from idea.md and support it with numbers "
        "from data-index.md, every one of which must appear there verbatim "
        "([MISSING DATA] otherwise). Do not introduce a new contribution here, and "
        "do not restate the full method design."
    ),
}


def build_routing_clause(route: dict, family: str = "", part: dict = None) -> str:
    """One paragraph telling the drafter which evidence source is primary.

    idea.md 与 data-index.md 已在提示词第一行点明;这里再陈述一遍规则,因为要防的
    失败模式(方法章写成结果回顾)正是模型在两个来源都在手时容易滑入的。

    `family` 覆盖章级 family(用于分段路由);`part` 把"横跨多种类型"的提示缩到该段
    自己覆盖的小节。
    """
    family = family or route.get("family", MIXED)
    label = route.get("type", "unknown")
    section_types = route.get("section_types") or {}
    scope = ([n for n in part.get("numbers", []) if n in section_types]
             if part else sorted(section_types))
    if part and scope:
        # 点名本段自己的小节,不是整篇的。
        label = "/".join(dict.fromkeys(section_types[n] for n in scope))
    clause = ROUTING_CLAUSES.get(family, ROUTING_CLAUSES[MIXED]).format(type=label)
    if len({section_types[n] for n in scope}) > 1:
        per_section = ", ".join(f"section {n} is {section_types[n]}" for n in scope)
        clause += (f" This covers several section types ({per_section}); apply the "
                   f"rule above per section rather than uniformly.")
    return clause + "\n\n"


REVIEW_ROUTING_CLAUSES = {
    IDEA: (
        "This is a {type} chapter — an IDEA chapter, judged on whether the "
        "contribution is explained, not on how much data it reports. Raise MUST FIX "
        "when the mechanism is asserted rather than explained, when a design choice "
        "is unmotivated, or when the chapter drifts into reporting results instead "
        "of arguing the idea. Do NOT demand more experimental numbers here, and do "
        "NOT treat an absent metric as a defect — the results chapter carries that. "
        "A [DESIGN DETAIL NEEDED] marker is the correct behaviour when the author's "
        "idea document is silent; flag it for the author instead of asking the "
        "drafter to invent the detail."
    ),
    DATA: (
        "This is a {type} chapter — a DATA chapter, judged on whether the reported "
        "evidence is complete, fairly compared, and traceable to the results. Raise "
        "MUST FIX for any number not present in data-index.md, missing baselines or "
        "ablations, absent variance/seed information, and claims stronger than the "
        "data supports. Do not ask for more exposition of the method design here."
    ),
    MIXED: (
        "This is a {type} chapter — it must connect the contribution to the "
        "evidence. Raise MUST FIX when a claim about the idea is unsupported by the "
        "results, when a number does not appear in data-index.md, or when the "
        "chapter introduces a contribution the author never claimed."
    ),
}


def build_review_routing_clause(route: dict) -> str:
    """Tell the reviewer what standard applies to this chapter type.

    Without it the reviewer applies experiment-paper criteria uniformly and fills
    a Method chapter's MUST FIX list with demands for statistics it was never
    supposed to report — which the convergence loop then treats as a frozen
    acceptance checklist and burns rounds trying to satisfy.
    """
    if PAPER_MODE != "experiment":
        return ""
    family = route.get("family", MIXED)
    clause = REVIEW_ROUTING_CLAUSES.get(family, REVIEW_ROUTING_CLAUSES[MIXED]).format(
        type=route.get("type", "unknown"))
    if route.get("gate") == ADVISORY:
        clause += (" The number gate is ADVISORY for this chapter: an empty or partial "
                   "results store is not itself a defect.")
    elif route.get("gate") == OFF:
        clause += (" The number gate is OFF for this chapter: it is prose, and reporting "
                   "no metrics is correct.")
    return clause + "\n\n"


# 内容随路由变化的下游产物,按流水线顺序排列。路由变化由 brief.md 首行的 outline
# 指纹(chapter_fingerprint,覆盖 type + 小节级 type + 要点)统一捕获:改了 brief 的
# `type:` 后,read_brief_fingerprint 与当前 chapter_fingerprint 不符即硬停。真正的陷阱
# 在指纹通过之后的下游产物:evidence-pack.md(提问视角随 family 变)、draft-v1.plan.md
# 与各 part(路由子句随 family 变)、number-check.md(严格度随 gate 变)、review-v1.*
# (判据随 family 变)都带"存在即跳过"的断点续跑逻辑。brief 指纹硬停时,这些文件会以
# 旧路由的身份被静默复用——方法章于是拿着结果章的证据包继续写,而流水线全程显示"成功"。
# 所以 brief 指纹一硬停就把这些陈旧产物一并列出,提示作者清掉再重跑。
# todo.md / decision.md 不在清单内:可能含人工手写内容。
ROUTE_DEPENDENT_ARTIFACTS = [
    "evidence-pack.md",
    "draft-v1.plan.md",
    "draft-v1.part-1.md", "draft-v1.part-2.md", "draft-v1.part-3.md",
    "draft-v1.md",
    "number-check.md",
    "review-v1.md", "review-v1.json",
    "citation-insertions.md",
    "draft-v2.md",
    "final.md", "final.zh.md",
]
# 收敛循环的轮次产物:orchestrator 对它们也有"存在即跳过"的逻辑,路由变了同样
# 会被静默复用,甚至把旧路由的 round draft 提升为 draft-v2.md。数量不固定,用 glob。
ROUTE_DEPENDENT_ARTIFACT_GLOBS = [
    "draft-v2.round-*.md",
    "review-verify.round-*.json",
]


def warn_stale_route_artifacts(folder_path: str, folder_rel: str,
                               old_fingerprint, new_fingerprint: str) -> list[str]:
    """路由(brief 指纹)变了就报警,并列出仍留在盘上的旧路由产物。返回陈旧文件名列表。

    old_fingerprint / new_fingerprint 是 brief.md 的 outline 指纹(chapter_fingerprint)。
    只提示不删除:这些产物是真金白银的 token 换来的,是否作废由使用者决定。
    """
    folder = Path(folder_path)
    stale = [name for name in ROUTE_DEPENDENT_ARTIFACTS
             if (folder / name).exists()]
    for pattern in ROUTE_DEPENDENT_ARTIFACT_GLOBS:
        stale.extend(p.name for p in sorted(folder.glob(pattern)) if p.is_file())

    print(f"[Manager  ] route-chg  | brief.md 的 outline 指纹已改变:", flush=True)
    print(f"[Manager  ] route-chg  |   旧: {old_fingerprint}", flush=True)
    print(f"[Manager  ] route-chg  |   新: {new_fingerprint}", flush=True)
    if not stale:
        print(f"[Manager  ] route-chg  | 无陈旧产物,本次将按新路由完整重跑。", flush=True)
        return stale
    print(f"[Manager  ] STALE      | 下列产物按旧路由生成,断点续跑会直接复用:", flush=True)
    for name in stale:
        print(f"[Manager  ] STALE      |   {folder_rel}/{name}", flush=True)
    print(f"[Manager  ] STALE      | 想按新路由重写,请先删除上述文件再重跑;"
          f"想保留就当心它们的证据来源与判据仍是旧类型的。", flush=True)
    return stale


def run_agent_stage_standalone(agent, agent_name: str, prompt: str,
                               attempts: int = 3):
    """在流水线之外跑一次 Agent(如 `--expand` 的 outline 展开)。

    与流水线内的 run_agent_stage 同样带重试与进度过滤,但不依赖章节工作区的闭包
    变量。重试次数默认少一次:这一步没有已落盘的产物要保护,失败重跑一条命令就行。
    永久错误（401/403/413/422）立即失败，不重跑。
    """
    last_error = None
    for attempt in range(1, attempts + 1):
        print(f"[{agent_name:<8.8}] running    | model request in progress "
              f"(attempt {attempt}/{attempts})", flush=True)
        try:
            seen_progress = set()
            events = []
            visible_stdout = os.sys.stdout
            # standalone 单次调用(如 --expand)没有 Stage 表格,加心跳让用户看到
            # Manager 还在推理,而不是长时间无输出像卡住。
            filtered = ProgressFilteringStdout(agent_name, seen_progress,
                                               visible_stdout, heartbeat=True)
            with contextlib.redirect_stdout(filtered):
                try:
                    stream = agent.run(prompt, stream=True)
                except TypeError:
                    return agent.run(prompt)
                for event in stream:
                    events.append(event)
                    content = getattr(event, "content", None)
                    if content is not None:
                        print_stream_progress(content, seen_progress, agent_name,
                                              visible_stdout)
            # 心跳行用 \r 覆盖,结束时清掉并换行,避免残留一行"thinking"挡住后续输出
            if filtered._last_heartbeat > 0:
                print("\r" + " " * 72 + "\r", end="", flush=True)
            return events[-1] if events else None
        except Exception as exc:
            last_error = exc
            # 永久错误立即失败，不重跑（重跑只会再撞同一个永久错误）。
            if _is_permanent_failure(exc):
                print(f"[{agent_name:<8.8}] failed     | permanent error, no retry: "
                      f"{_safe_exc_label(exc)}", flush=True)
                return {"error": "permanent error", "exception_type": type(exc).__name__}
            # 安全标签：只取类名+status_code，不拼 str(exc)（防 key/prompt 泄漏到日志）
            label = _safe_exc_label(exc)
            if attempt >= attempts:
                print(f"[{agent_name:<8.8}] failed     | {label}", flush=True)
                return {"error": label, "exception_type": type(exc).__name__}
            kind = ("transient API/network error"
                    if TRANSIENT_ERROR_RE.search(str(exc))
                    else "error")
            print(f"[{agent_name:<8.8}] retry      | {kind}: {label}", flush=True)
    return {"error": _safe_exc_label(last_error) if last_error else "unknown error"}


# ── idea.md 直连:每个改写正文的 stage 提示词的固定前缀 ──────────────────
# idea 是全局一份、所有章节共读的最高优先输入。不再复制进任何聚合文件:每个 stage
# 提示词第一行就指向 idea.md,Agent 用 read_file 读原文全文。这避免两个漂移源——
# 上游改写 idea(论点走样)、idea 在 data 章被降级成"背景"。data 类章节额外点明
# data-index.md 作为数字导航;纯论述章(related/background)只读 idea。
def idea_clause(family: str = MIXED) -> str:
    """注入每个正文 stage 提示词第一行:先读 idea,数据章再读 data-index。"""
    head = (
        f"FIRST read idea.md in full ({IDEA_PATH}) — the author's own statement of the "
        f"contribution (novelty, mechanism, method design). It is the highest-priority "
        f"input: write the contribution from it verbatim, do not paraphrase or water it "
        f"down, and never add a contribution the author did not make. ")
    if family in (DATA, MIXED):
        head += (
            f"THEN read '{DATA_INDEX_PATH}' — the Manager-built three-level index into "
            f"data/ (experiment → result → specific value). Every number you cite must "
            f"appear there; if one is absent write [MISSING DATA] rather than guessing. ")
    return head


def cross_chapter_state_has_claim(text: str, chapter: str) -> bool:
    """跨章状态结构完好,且本章在 **Key Claims 小节内**有 Stage 5 标记。

    必须限定在小节内:文件顶部有一份章节顺序清单,里面本来就写着每个文件夹名。
    全文搜 `- [<章名>] ` 会把那份清单当成交接凭证,于是 Stage 5 完全没跑也判成
    成功——这套校验存在的意义就没了。
    """
    starts = []
    for heading in XCHAP_HEADINGS:
        if text.count(heading) != 1:
            return False
        starts.append(text.index(heading))
    if starts != sorted(starts):
        return False
    claims = text[starts[1] + len(XCHAP_HEADINGS[1]):starts[2]]
    return f"- [{chapter}] " in claims


def cross_chapter_state_preserves_others(before: str, after: str,
                                         chapter: str) -> bool:
    """Stage 5 的候选文件有没有动到**别的章**。

    只允许两种改动:增删本章自己的 `- [<本章>] ` 条目,以及在小节内插入新行。前几章
    积累的术语约定是后续每一章对齐的唯一依据,被静默删掉时下游全部各写一套,而
    Stage 5 自己会显示成功——所以判据是"旧内容的每一行都还在",不是"行数没变少"。

    比较时忽略空行:Agent 重排小节内的空行是无害的格式变化,拿它当失败会让这道
    校验频繁误报,最后只能被关掉。
    """
    prefix = f"- [{chapter}] "

    def meaningful(text: str) -> list[str]:
        return [line for line in text.splitlines()
                if line.strip() and not line.startswith(prefix)]

    kept = meaningful(after)
    # 逐行判存在而不是整表相等:允许在中间插入本章的新条目,不允许旧行消失或被改写。
    return all(line in kept for line in meaningful(before))


def _assert_survey_mode(fn_name: str) -> None:
    """在非 survey 模式下调用 survey 专用路径时给出明确提示并中止。

    run.py 已在入口层拦截 experiment 模式下的非 --progress 调用，这里是第二道
    防线：防止将来有代码绕过 run.py 直接调用这些函数，导致 Agent 读取本项目里
    不存在的 `paper/00 Background & Example/` 目录而静默失败。
    """
    if PAPER_MODE != "survey":
        raise RuntimeError(
            f"{fn_name}() is for PAPER_MODE=survey only. "
            f"Current mode is '{PAPER_MODE}'. "
            f"Use run_4stage_with_progress() in experiment mode."
        )


def build_4stage_manager_prompt(folder_path: str) -> str:
    _assert_survey_mode("build_4stage_manager_prompt")
    folder_path = folder_path.replace("\\", "/")
    chapter = chapter_name_from_folder(folder_path)
    reference_excerpt = build_reference_excerpt(chapter)
    return f"""\
Execute a 4-stage paper writing iteration for folder: {folder_path}

Before starting, note the cross-chapter context files:
- paper/00 Background & Example/cross-chapter-state.md (read for terminology and prior chapter summaries)
- paper/01 Structure/final.md (read for the paper's logic line)
- filtered reference excerpt below (use for primary and cross-chapter literature reuse)
- Do not read references/index.md directly; call search_references(query, chapter) only for bounded lookup.

Terminal output rules:
- Print concise progress only, with an explicit owner prefix and relative paths: `[Manager] Stage 1/4 | Calling draft_agent`, `[Manager] Tool      | read_file -> paper/...`, `[Manager] Tool      | write_file -> paper/...`, and `[Manager] Verify    | draft-v1.md exists`.
- Do not print draft prose, review prose, full note contents, full file contents, or long generated markdown to the terminal.
- Let sub-agents write content to files; summarize only file names and status in terminal output.

Stage 1 - Draft:
  Call draft_agent with task: "Read brief.md, input.md, and cross-chapter context from '{folder_path}'. Also read 'paper/00 Background & Example/cross-chapter-state.md' and 'paper/01 Structure/final.md'. Use the filtered reference excerpt injected below to identify primary and cross-chapter papers, then open listed Note Path files only when needed. Do not read references/index.md directly. Write a complete academic draft to '{folder_path}/draft-v1.md' and '{folder_path}/todo.md'."

Stage 2 - Review:
  Call review_agent with task: "Read draft-v1.md, brief.md, and input.md from '{folder_path}'. Also read 'paper/00 Background & Example/cross-chapter-state.md' and 'paper/01 Structure/final.md'. Use the filtered reference excerpt injected below to check whether important primary or cross-chapter references were missed. Do not read references/index.md directly. Write a detailed review to '{folder_path}/review-v1.md' and '{folder_path}/todo.md'. Categorize issues as MUST FIX / SHOULD FIX / CONSIDER."

Stage 3 - Revise:
  Call draft_agent with task: "Read brief.md, input.md, draft-v1.md, and review-v1.md from '{folder_path}'. Also read 'paper/00 Background & Example/cross-chapter-state.md' and 'paper/01 Structure/final.md'. Use the filtered reference excerpt injected below. Do not read references/index.md directly. Address ALL 'MUST FIX' items, including missing reference reuse when supported by notes. Write improved draft to '{folder_path}/draft-v2.md' and '{folder_path}/todo.md'. If major rewrite, also write '{folder_path}/decision.md'."

Stage 4 - Finalize:
  Call review_agent with task: "Read draft-v1.md, draft-v2.md, review-v1.md, brief.md, and input.md from '{folder_path}'. Also read 'paper/00 Background & Example/cross-chapter-state.md' and 'paper/01 Structure/final.md'. Use the filtered reference excerpt injected below. Do not read references/index.md directly. Merge the best parts of both drafts, resolving all issues and preserving valid cross-chapter reference reuse. Write '{folder_path}/final.md' as the English publication-ready version, '{folder_path}/final.zh.md' as a Chinese reading/review version for the user, '{folder_path}/decision.md' (explaining what was kept/dropped), and '{folder_path}/todo.md' (remaining issues)."

After each stage, call list_folder on '{folder_path}' to verify the output files exist.
After Stage 4 completes, read '{folder_path}/final.md' and '{folder_path}/todo.md', then update 'paper/00 Background & Example/cross-chapter-state.md' with new terminology decisions, a one-sentence chapter summary, and any unresolved cross-chapter issues.
Report completion status for each stage.

Filtered reference excerpt for this chapter:

{reference_excerpt}
"""


def run_4stage_via_manager(manager_agent, folder_path: str) -> str:
    """Execute 4-stage iteration via Manager Agent (CodeAgent orchestration).

    The manager generates Python code to call draft_agent and review_agent
    in sequence, verifying outputs at each stage.

    survey 专用 — experiment 模式请使用 run_4stage_with_progress()。
    """
    _assert_survey_mode("run_4stage_via_manager")
    return manager_agent.run(build_4stage_manager_prompt(folder_path))


def run_4stage_via_manager_stream(manager_agent, folder_path: str, raw: bool = False) -> list:
    _assert_survey_mode("run_4stage_via_manager_stream")
    events = []
    seen_progress = set()
    for event in manager_agent.run(build_4stage_manager_prompt(folder_path), stream=True):
        content = getattr(event, "content", None)
        if raw:
            print(content if content is not None else event, end="" if content is not None else "\n", flush=True)
        elif content is not None:
            print_stream_progress(content, seen_progress, include_stage_hints=True)
        else:
            text = str(event)
            if ERROR_RE.search(text):
                print(f"[Manager] Notice    | {sanitize_progress_text(text[:220])}", flush=True)
        events.append(event)
    return events


def run_4stage_direct(draft_agent, review_agent, folder_path: str) -> dict:
    """Execute 4-stage iteration via direct Python orchestration.

    Calls sub-agents sequentially without the Manager Agent.
    More predictable but less flexible.
    Note: cross-chapter-state.md update must be done manually after this completes.

    ⚠️  Survey mode only — experiment mode must use run_4stage_with_progress().
    """
    _assert_survey_mode("run_4stage_direct")
    folder_path = folder_path.replace("\\", "/")
    results = {}

    cross_chapter = "paper/00 Background & Example/cross-chapter-state.md"
    structure = "paper/01 Structure/final.md"
    chapter = chapter_name_from_folder(folder_path)
    reference_excerpt = build_reference_excerpt(chapter)

    print(f"\n[Stage 1/4] Drafting v1...")
    results["stage1_draft"] = draft_agent.run(
        f"Read brief.md and input.md from '{folder_path}'. "
        f"Also read '{cross_chapter}' and '{structure}'. Do not read references/index.md directly; use the filtered reference excerpt below. "
        f"Write a complete academic draft to '{folder_path}/draft-v1.md' "
        f"and '{folder_path}/todo.md' listing known gaps.\n\n"
        f"{reference_excerpt}"
    )

    if not os.path.exists(os.path.join(folder_path, "draft-v1.md")):
        print("[ERROR] draft-v1.md was not created. Aborting.")
        return results

    print(f"\n[Stage 2/4] Reviewing v1...")
    results["stage2_review"] = review_agent.run(
        f"Read draft-v1.md and brief.md from '{folder_path}'. "
        f"Also read '{cross_chapter}' and '{structure}'. Do not read references/index.md directly; use the filtered reference excerpt below for terminology consistency and missing reference reuse. "
        f"Write a detailed review to '{folder_path}/review-v1.md' and '{folder_path}/todo.md'. "
        f"Categorize issues as MUST FIX / SHOULD FIX / CONSIDER.\n\n"
        f"{reference_excerpt}"
    )

    if not os.path.exists(os.path.join(folder_path, "review-v1.md")):
        print("[ERROR] review-v1.md was not created. Aborting.")
        return results

    print(f"\n[Stage 3/4] Revising (draft v2)...")
    results["stage3_revise"] = draft_agent.run(
        f"Read brief.md, input.md, draft-v1.md, and review-v1.md from '{folder_path}'. "
        f"Also read '{cross_chapter}' and '{structure}'. Do not read references/index.md directly; use the filtered reference excerpt below. "
        f"Address ALL 'MUST FIX' items from the review. "
        f"Write improved draft to '{folder_path}/draft-v2.md' and '{folder_path}/todo.md'. "
        f"If this is a major rewrite, also write '{folder_path}/decision.md' explaining trade-offs.\n\n"
        f"{reference_excerpt}"
    )

    if not os.path.exists(os.path.join(folder_path, "draft-v2.md")):
        print("[ERROR] draft-v2.md was not created. Aborting.")
        return results

    print(f"\n[Stage 4/4] Finalizing...")
    results["stage4_final"] = review_agent.run(
        f"Read draft-v1.md, draft-v2.md, review-v1.md, brief.md, and input.md from '{folder_path}'. "
        f"Also read '{cross_chapter}' and '{structure}'. Do not read references/index.md directly; use the filtered reference excerpt below. "
        f"Merge the best parts of both drafts, resolving all issues. "
        f"Write '{folder_path}/final.md' as the English publication-ready version, "
        f"'{folder_path}/final.zh.md' as a Chinese reading/review version for the user, "
        f"'{folder_path}/decision.md' (what was kept/dropped and why), "
        f"and '{folder_path}/todo.md' (remaining issues).\n\n"
        f"{reference_excerpt}"
    )

    if os.path.exists(os.path.join(folder_path, "final.md")):
        print("\n[DONE] All 4 stages completed successfully.")
        print("[NOTE] Remember to update cross-chapter-state.md manually or re-run with Manager mode.")
    else:
        print("\n[WARNING] final.md was not created.")

    return results


def run_4stage_with_progress(draft_agent, review_agent, folder_path: str, manager_agent=None) -> dict:
    """4-stage run with fully Python-controlled, human-readable progress output.

    All smolagents raw streaming is disabled; every visible line is written by this
    function or the shared tool wrappers (read_file / write_file / list_folder).
    """
    folder_path = folder_path.replace("\\", "/")
    folder_rel = display_path(folder_path)
    results = {}

    chapter = chapter_name_from_folder(folder_path)
    # C6: in experiment mode do not inject the survey reference index — the
    # experiment's context source is the results store, not literature rows.
    # The bibliography is still available via search_references / the reader tool.
    reference_excerpt = ("" if PAPER_MODE == "experiment"
                         else build_reference_excerpt(chapter))

    # Chapter-type routing. Parsed up front because the declared type decides what
    # the prompts point at (idea.md vs data-index.md) and whether the number gate
    # may block. An idea chapter drafted from the results store is the
    # architecture-level failure this routing exists to prevent.
    sections = parse_brief_sections(folder_path)
    route = resolve_run_route(folder_path, sections) if PAPER_MODE == "experiment" else {
        "type": "survey", "family": MIXED, "gate": OFF, "source": "default"}
    family, gate = route["family"], route["gate"]

    # Mode-aware context sources. Survey mode reads the cross-chapter files; the
    # standalone experiment mode points every stage at idea.md (plus data-index.md
    # for data chapters) instead of synthesizing any pack. Both paths end up as
    # plain file paths the agents read, so downstream code is unchanged. Survey
    # behaviour is byte-identical when
    # PAPER_MODE == "survey".
    if PAPER_MODE == "experiment":
        print(f"[Manager  ] route      | {route_banner(route)}", flush=True)
        if route.get("unrecognized"):
            print(f"[Manager  ] notice     | brief.md declares unknown type "
                  f"'{route['unrecognized']}'; routing as mixed/advisory. "
                  f"Use one of: abstract, intro, related, background, method, theory, "
                  f"experiments, results, analysis, ablation, discussion, limitations, conclusion.",
                  flush=True)
        elif route.get("source") != "brief":
            print(f"[Manager  ] notice     | brief.md declares no 'type:'; inferred from "
                  f"{route.get('source')}. Add a 'type:' line to brief.md to make this explicit.",
                  flush=True)
        # Pre-flight: both primary sources are checked BEFORE any model call.
        # A chapter missing its primary source cannot be written, and discovering
        # that after Stage 0 + Stage 1 wastes four model calls on prose that the
        # gate will reject anyway.
        #
        # An idea-family chapter with no idea document has no primary source: the
        # contribution is the author's to state, not ours to invent.
        if family in (IDEA, MIXED):
            idea_missing = not Path(IDEA_PATH).is_file()
            idea_skeleton, idea_words = False, 0
            if not idea_missing:
                try:
                    from .content_source import load_idea_document, idea_is_skeleton
                    idea_skeleton, idea_words = idea_is_skeleton(load_idea_document())
                except Exception as exc:
                    print(f"[Manager  ] notice     | idea document check skipped: "
                          f"{str(exc)[:160]}", flush=True)
            if idea_missing or idea_skeleton:
                reason = (f"no idea document at {IDEA_PATH}" if idea_missing else
                          f"the idea document at {IDEA_PATH} is still the unfilled template "
                          f"({idea_words} words of authored text)")
                print(f"[Manager  ] FAIL      | idea-family chapter ({route['type']}) but "
                      f"{reason}", flush=True)
                print(f"[Manager  ] FAIL      | the novelty/method design is the primary source for "
                      f"this chapter and cannot be inferred from results.", flush=True)
                action = ("write" if idea_missing else "fill in")
                print(f"[Manager  ] FAIL      | {action} {Path(IDEA_PATH).name} "
                      f"(sections 3 and 4 matter most), then re-run.", flush=True)
                return _finish_with_table(folder_path, results)
        # Symmetrically: a blocking-gate chapter with no results store has nothing
        # to report. The post-draft number gate would catch this, but only after
        # paying for evidence mining, planning, and three draft parts.
        if gate == BLOCKING:
            try:
                from .number_gate import load_results_store
                if not load_results_store(str(DATA_ROOT)):
                    print(f"[Manager  ] FAIL      | data-family chapter ({route['type']}) but no "
                          f"experiment results under {DATA_ROOT}", flush=True)
                    print(f"[Manager  ] FAIL      | this chapter's content IS the numbers; there is "
                          f"nothing to report and every number would be unverifiable.", flush=True)
                    print(f"[Manager  ] FAIL      | provide data/ results (see data/README.md), "
                          f"then re-run. To draft the idea chapters first, run those instead.",
                          flush=True)
                    return _finish_with_table(folder_path, results)
            except Exception as exc:
                print(f"[Manager  ] notice     | pre-flight results check skipped: "
                      f"{str(exc)[:160]}", flush=True)
        # ── 写作路由 ────────────────────────────────────────────────
        # 所有章节文件夹都来自 outline.md,每一章都是整篇里的第 N 章:有邻章、有跨章
        # 状态、符号沿用前章。
        #
        # 解析失败必须硬停:不能退回"空的 mode_clause"——那等于没下任何写作指令,
        # 是比任何一种指令都糟的行为,所以这里直接停。
        xchap = os.path.join(str(Path(folder_path).parent), CROSS_CHAPTER_STATE)
        try:
            info = resolve_write_mode(chapter)
        except (OutlineRouteError, OSError) as exc:
            print(f"[Manager  ] FAIL      | 无法确定写作路由: {exc}", flush=True)
            print(f"[Manager  ] FAIL      | 所有章节都从 outline.md 生成;"
                  f"先跑 `python run.py --init`。", flush=True)
            results["route_blocked"] = f"not in outline: {exc}"
            return _finish_with_table(folder_path, results)

        mode_clause = build_mode_clause(chapter, cross_chapter_path=xchap)
        mode_review_clause = build_mode_review_clause(
            chapter, cross_chapter_path=xchap)
        # 邻章视野只给规划者:它需要知道前后章覆盖什么才能划清边界。有界——三章的
        # 标题与小节清单,不含要点正文,整篇 outline 不进任何 Agent 的上下文。
        outline_clause = build_outline_excerpt(chapter)
        cross_xchap_hint = ""
        if os.path.exists(xchap):
            cross_xchap_hint = (
                f"\nAlso read '{xchap}': it holds the terminology decisions and "
                f"key claims of the chapters already written. Reuse those "
                f"definitions verbatim instead of coining new ones.\n")
        detail = f"整篇第 {info['position']}/{info['total']} 章,可跨章引用"
        print(f"[Manager  ] write-mode | full — {detail}", flush=True)
        # brief.md 有两道门禁:① 非生成式 brief(无 outline 指纹);② outline 改动后
        # 已过期的 brief。chapter_fingerprint 覆盖 type + 小节级 type + 要点,所以"改了
        # brief 的 type:"这条路由此统一捕获——不需要再有一份独立的证据路由指纹。
        # 两种都硬停而不是报警继续:报警继续意味着这一次就用错的写作契约生成正文,而且
        # 下一次运行指纹已被覆盖、警告不再出现,错误产物从此静默传递下去。硬停不删任何
        # 文件,但会把下游陈旧产物一并列出(它们都带"存在即跳过",不清掉会被旧路由复用)。
        brief_fp = read_brief_fingerprint(os.path.join(folder_path, "brief.md"))
        if not brief_fp:
            results["route_blocked"] = "chapter has a non-generated brief"
            print(f"[Manager  ] FAIL      | '{chapter}' 的 brief.md 不是 --init 生成的"
                  f"(缺少 outline 指纹)", flush=True)
            print(f"[Manager  ] FAIL      | 非生成式 brief 没有章序号与结构约定,"
                  f"按整篇跑会写出错的衔接。先跑 `python run.py --init --force`", flush=True)
            return _finish_with_table(folder_path, results)
        if chapter_fingerprint(info["chapter"]) != brief_fp:
            results["route_blocked"] = "brief is stale against outline"
            results["stale_route_artifacts"] = warn_stale_route_artifacts(
                folder_path, folder_rel, brief_fp, chapter_fingerprint(info["chapter"]))
            print(f"[Manager  ] FAIL      | outline.md 已改动,但本章 brief.md 还是旧版本", flush=True)
            print(f"[Manager  ] FAIL      | 继续跑用的是旧章节规格。先备份需要保留的内容,", flush=True)
            print(f"[Manager  ] FAIL      | 删掉上面列出的陈旧产物(brief.md 也在内),再跑本章。", flush=True)
            print(f"[Manager  ] FAIL      | 注意:不要只跑 `--init --force`——那只会刷新 brief 指纹,", flush=True)
            print(f"[Manager  ] FAIL      | 而下游 evidence-pack/plan/review 仍是旧路由的,会被静默复用。", flush=True)
            return _finish_with_table(folder_path, results)
        if not os.path.isfile(xchap):
            results["route_blocked"] = "chapter has no cross-chapter state"
            print(f"[Manager  ] FAIL      | {display_path(xchap)} 不存在;"
                  f"本章无处读取前章术语、也无处写下自己的约定", flush=True)
            print(f"[Manager  ] FAIL      | 跑 `python run.py --init` 生成它后重试", flush=True)
            return _finish_with_table(folder_path, results)

        from .content_source import content_source_summary
        print(f"[Manager  ] content    | {content_source_summary(family)}", flush=True)
        structure = f"{folder_path}/brief.md"
        routing_clause = build_routing_clause(route)
    else:
        structure = "paper/01 Structure/final.md"
        xchap = ""
        cross_xchap_hint = ""
        routing_clause = ""
        outline_clause = ""
        mode_clause = ""
        mode_review_clause = ""

    def verify(expected: list[str]) -> bool:
        for name in expected:
            full = os.path.join(folder_path, name)
            if os.path.exists(full):
                print(f"[Manager  ] verify     | {name} exists", flush=True)
            else:
                print(f"[Manager  ] MISSING    | {name} — stage may have failed", flush=True)
                return False
        return True

    def all_exist(expected: list[str]) -> bool:
        return all(os.path.exists(os.path.join(folder_path, name)) for name in expected)

    def run_agent_stage(agent, agent_name: str, prompt: str):
        # 4 次纯兜底（用户锁定）。注意：ResilientModel 已在 Model 层把瞬时网络错误
        # 吃掉（每个 logical call 最多 3 次 attempt），所以走到这里的失败几乎都是
        # TransientModelUnavailable（重试用尽）或真正的非网络异常。这里重跑整个
        # CodeAgent.run() 是最后手段，不是主恢复路径。
        attempts = 4
        last_error = None
        for attempt in range(1, attempts + 1):
            print(f"[{agent_name:<8.8}] running    | model request in progress (attempt {attempt}/{attempts})", flush=True)
            try:
                seen_progress = set()
                events = []
                visible_stdout = os.sys.stdout
                # 流水线内也开心跳:每步模型推理时显示"已接收 N 字",避免看似卡住。
                filtered_stdout = ProgressFilteringStdout(agent_name, seen_progress,
                                                          visible_stdout, heartbeat=True)
                with contextlib.redirect_stdout(filtered_stdout):
                    try:
                        stream = agent.run(prompt, stream=True)
                    except TypeError:
                        return agent.run(prompt)
                    for event in stream:
                        events.append(event)
                        content = getattr(event, "content", None)
                        if content is not None:
                            print_stream_progress(content, seen_progress, agent_name, visible_stdout)
                # 心跳行用 \r 覆盖,结束时清掉并换行,避免残留一行"thinking"挡住后续输出
                if filtered_stdout._last_heartbeat > 0:
                    print("\r" + " " * 72 + "\r", end="", flush=True)
                return events[-1] if events else None
            except Exception as exc:
                last_error = exc
                # 永久错误（401/403/413/422 等）不应重跑整个 CodeAgent——重跑只会
                # 再次撞同一个永久错误，白白烧 token + 时间。沿异常链找根因：
                # ResilientModel 用 raise ... from last_exc，永久错误的原始异常在
                # __cause__/__context__ 里，status_code 落在永久集合就立即失败。
                if _is_permanent_failure(exc):
                    print(f"[{agent_name:<8.8}] failed     | permanent error, no retry: "
                          f"{_safe_exc_label(exc)}", flush=True)
                    return {"error": "permanent error", "exception_type": type(exc).__name__}
                # 安全标签：只取类名+status_code，不拼 str(exc)（防 key/prompt 泄漏）
                label = _safe_exc_label(exc)
                if attempt >= attempts:
                    print(f"[{agent_name:<8.8}] failed     | {label}", flush=True)
                    return {"error": label, "exception_type": type(exc).__name__}
                # TransientModelUnavailable 是 ResilientModel 重试用尽抛的——重跑整个
                # CodeAgent 是合理的最后兜底（可能换个时间段网关恢复）。其它异常也照
                # 常重试：CodeAgent 内部解析错/执行错本身就是 AgentError，重跑可能换条路径。
                if TRANSIENT_ERROR_RE.search(str(exc)) or "TransientModelUnavailable" in type(exc).__name__:
                    print(f"[{agent_name:<8.8}] retry      | transient API/network error: {label}", flush=True)
                else:
                    print(f"[{agent_name:<8.8}] retry      | {label}", flush=True)
        return {"error": _safe_exc_label(last_error) if last_error else "unknown error"}

    # ── Stage 0: Evidence mining (pre-draft, multi-perspective QA) ──
    # STORM's perspective-guided QA: interrogate the content source before
    # drafting so the draft is written against grounded evidence, not an outline.
    # Experiment mode only. 两步:① 先由 Manager 读参考文献生成本章 input.md(--init 不
    # 再生成空骨架);② 再由 Draft 跑多视角问答写 evidence-pack.md。两步都可续跑
    # (存在即跳过)。证据边界是 idea.md(全局直读)+ data-index.md(数据章导航),
    # 不再依赖任何聚合文件。
    if PAPER_MODE == "experiment":
        try:
            from .evidence_mining import run_input_material, run_evidence_mining
            if manager_agent is not None:
                results["stage0_input"] = run_input_material(
                    manager_agent, folder_path, chapter, family,
                    run_agent_stage, set_agent_context, verify)
            print(f"\n[Manager  ] Stage 0/4  | Evidence mining ({family} perspectives) "
                  f"→ {folder_rel}/evidence-pack.md", flush=True)
            results["stage0_evidence"] = run_evidence_mining(
                draft_agent, folder_path, chapter,
                reference_excerpt, run_agent_stage, verify, set_agent_context,
                family=family,
            )
        except Exception as exc:
            print(f"[Manager  ] notice     | evidence mining skipped: {str(exc)[:160]}", flush=True)

    # ── Stage 1: Draft ──────────────────────────────────────────────
    # `sections` was parsed above for chapter-type routing; reuse it.
    stage1_parts = build_stage1_parts(sections)
    part_count = len(stage1_parts)
    stage1_plan = format_stage1_parts(stage1_parts)
    if not sections:
        print(f"[Manager  ] WARN       | brief.md 没解析出小节(标题行须形如 "
              f"`1. **标题** (~250 words)`);退回单段起草", flush=True)

    print(f"\n[Manager  ] Stage 1/4  | Draft plan → {folder_rel}/draft-v1.plan.md", flush=True)
    if os.path.exists(os.path.join(folder_path, "draft-v1.plan.md")):
        print("[Manager  ] skip       | draft-v1.plan.md exists", flush=True)
        results["stage1_plan"] = "skipped"
    else:
        planner = manager_agent or draft_agent
        planner_name = "Manager" if manager_agent is not None else "Draft"
        set_agent_context(planner_name)
        evidence_clause = ""
        if PAPER_MODE == "experiment" and os.path.exists(os.path.join(folder_path, "evidence-pack.md")):
            subject = {IDEA: "the idea and its mechanism", DATA: "the results"}.get(
                family, "the idea and the results")
            evidence_clause = (
                f"Also read '{folder_path}/evidence-pack.md': it contains the hard questions "
                f"each perspective posed about {subject} and the grounded answers. Use its "
                f"grounded answers to pick target claims; treat its '## Open Gaps' as drafting "
                f"caveats, not to be silently ignored. ")
        results["stage1_plan"] = run_agent_stage(planner, planner_name, (
            f"{idea_clause(family) if PAPER_MODE == 'experiment' else ''}"
            f"You are acting only as the planner for Stage 1. "
            f"Read brief.md and input.md from '{folder_path}'. "
            f"Also read '{structure}'. "
            f"{routing_clause}"
            f"{evidence_clause}"
            f"{cross_xchap_hint}"
            f"{mode_clause}"
            f"{outline_clause}"
            f"Do not read references/index.md directly; use the filtered reference excerpt below. "
            f"Write a concrete construction plan to '{folder_path}/draft-v1.plan.md' for exactly "
            f"{part_count} draft part{'s' if part_count > 1 else ''}. "
            f"Use these Python-fixed part boundaries; do not move sections across parts:\n\n{stage1_plan}\n\n"
            f"Begin the plan with a '## Notation and Terminology Table' that fixes, for the whole chapter: "
            f"the canonical spelling of each key term (pick ONE variant, e.g. 'fine-tuning' not 'finetuning'); "
            f"every abbreviation with its first-use expansion; and any mathematical symbol with its definition. "
            f"All {part_count} part{'s' if part_count > 1 else ''} must obey this table verbatim, "
            f"so keep it explicit and unambiguous. "
            f"For each part, specify target claims, required REF IDs, forbidden overlap, transition role, and output file. "
            f"Also write '{folder_path}/todo.md' for known planning gaps. Do not write draft prose.\n\n"
            f"{reference_excerpt}"
        ))
        set_agent_context("Manager")
        if not verify(["draft-v1.plan.md", "todo.md"]):
            return _finish_with_table(folder_path, results)

    results["stage1_parts"] = []
    for position, part in enumerate(stage1_parts):
        print(f"\n[Manager  ] Stage 1{chr(96 + part['index'])}/4 | Draft part {part['index']} → {folder_rel}/{part['output']}", flush=True)
        if os.path.exists(os.path.join(folder_path, part["output"])):
            print(f"[Manager  ] skip       | {part['output']} exists", flush=True)
            results["stage1_parts"].append("skipped")
            continue
        prev_context = ""
        if position > 0:
            prev_output = stage1_parts[position - 1]["output"]
            prev_tail = tail_text(os.path.join(folder_path, prev_output), 1200)
            if prev_tail:
                prev_context = (
                    f"PREVIOUS CONTEXT — the ending of Part {position} (already written). "
                    f"Continue seamlessly from it: do not repeat it, do not contradict its definitions, "
                    f"and keep terminology and symbols identical:\n\"\"\"\n{prev_tail}\n\"\"\"\n\n"
                )
        set_agent_context("Draft")
        evidence_draft_clause = ""
        if PAPER_MODE == "experiment" and os.path.exists(os.path.join(folder_path, "evidence-pack.md")):
            evidence_draft_clause = (
                f"If you need grounded details behind a claim, use read_file on "
                f"'{folder_path}/evidence-pack.md' and read ONLY the Q&A for your part's topics — "
                f"do not inline the whole pack. "
            )
        result = run_agent_stage(draft_agent, "Draft", (
            f"{idea_clause(part_family(part, route)) if PAPER_MODE == 'experiment' else ''}"
            f"Read brief.md, input.md, and draft-v1.plan.md from '{folder_path}'. "
            f"{build_routing_clause(route, part_family(part, route), part) if PAPER_MODE == 'experiment' else ''}"
            f"{mode_clause}"
            f"{evidence_draft_clause}"
            f"Do not read references/index.md directly; use the filtered reference excerpt below. "
            f"Obey the 'Notation and Terminology Table' in draft-v1.plan.md verbatim: one canonical spelling per term, "
            f"fixed abbreviation expansions, fixed symbol definitions. Never redefine a term or symbol fixed by an earlier part. "
            f"Write only Part {part['index']} to '{folder_path}/{part['output']}'. "
            f"Part boundary:\n{format_stage1_parts([part])}\n\n"
            f"{prev_context}"
            f"Follow draft-v1.plan.md for REF IDs, forbidden overlap, and transitions. "
            f"Do not write other parts and do not duplicate material assigned to other parts.\n\n"
            f"{reference_excerpt}"
        ))
        results["stage1_parts"].append(result)
        set_agent_context("Manager")
        if not verify([part["output"]]):
            return _finish_with_table(folder_path, results)

    concatenate_stage1_parts(folder_path, len(stage1_parts))
    print(f"[Manager  ] write_file | {folder_rel}/draft-v1.md", flush=True)
    if not verify(["draft-v1.md", "todo.md"]):
        return _finish_with_table(folder_path, results)

    # ── Deterministic number gate (experiment mode) ─────────────────
    # Cross-check every number in draft-v1 against the results store BEFORE the
    # expensive LLM review. Findings go to number-check.md so the reviewer folds
    # them into its MUST FIX list. Two failure modes:
    #   - MISMATCH (draft contradicts a value in the store): non-blocking, folded
    #     into the review as MUST FIX.
    #   - BLOCKING (no results store / no draft): fail closed — an experiment
    #     paper with no data to verify against is unbuildable, so we abort.
    if PAPER_MODE == "experiment" and gate != OFF:
        try:
            from .number_gate import run_number_gate
            passed, messages = run_number_gate(
                os.path.join(folder_path, "draft-v1.md"), str(DATA_ROOT))
            no_store = not passed and any(
                "[NUMBER-GATE] no results found" in m or "cannot verify numbers" in m
                or "every number in the draft is unverifiable" in m for m in messages)
            report = "# Number consistency check (draft-v1 vs results store)\n\n"
            report += f"Gate level: {gate} (chapter type: {route['type']})\n"
            report += f"Status: {'PASS' if passed else 'MISMATCH — treat as MUST FIX'}\n\n"
            report += "\n".join(f"- {m}" for m in messages) + "\n"
            Path(os.path.join(folder_path, "number-check.md")).write_text(report, encoding="utf-8")
            tag = "OK" if passed else "MISMATCH"
            print(f"[Manager  ] number-gate| {gate}: {tag} → {folder_rel}/number-check.md", flush=True)
            if no_store and gate == BLOCKING:
                # Blocking condition: this chapter's content IS the numbers, and
                # there are none. Do not proceed into an LLM review that would
                # argue about unverifiable values.
                print(f"[Manager  ] FAIL      | number gate is BLOCKING for a {route['type']} chapter: "
                      f"no experiment results to verify against", flush=True)
                print(f"[Manager  ] FAIL      | provide data/ results first, then re-run.", flush=True)
                return _finish_with_table(folder_path, results)
            if no_store and gate == ADVISORY:
                # An idea chapter may legitimately precede the experiments. Say so
                # once and continue — but any number it does write is unverified.
                print(f"[Manager  ] number-gate| advisory: no results store yet; "
                      f"numbers in this {route['type']} chapter are UNVERIFIED", flush=True)
        except Exception as exc:
            print(f"[Manager  ] notice     | number gate skipped: {str(exc)[:160]}", flush=True)
    elif PAPER_MODE == "experiment":
        print(f"[Manager  ] number-gate| off (chapter type: {route['type']} — prose only)", flush=True)

    # ── Stage 2: Review (Markdown + structured JSON checklist) ──────
    print(f"\n[Manager  ] Stage 2/4  | Review → {folder_rel}/review-v1.md (+ review-v1.json)", flush=True)
    if all_exist(["review-v1.md", "review-v1.json"]):
        print("[Manager  ] skip       | review-v1.md/review-v1.json exist", flush=True)
        results["stage2"] = "skipped"
    else:
        set_agent_context("Review")
        number_check_clause = ""
        if PAPER_MODE == "experiment" and os.path.exists(os.path.join(folder_path, "number-check.md")):
            number_check_clause = (
                f"Also read '{folder_path}/number-check.md': it is a deterministic cross-check of "
                f"every number in the draft against the results store. Treat each [NUMBER-MISMATCH] "
                f"line as a MUST FIX item (the prose contradicts the recorded results). "
            )
        results["stage2"] = run_agent_stage(review_agent, "Review", (
            f"{idea_clause(family)}"
            f"Read draft-v1.md, brief.md, input.md from '{folder_path}'. "
            f"Also read '{structure}'. Do not read references/index.md directly; use the filtered reference excerpt below. "
            f"{build_review_routing_clause(route)}"
            f"{mode_review_clause}"
            f"{number_check_clause}"
            f"Write a detailed review to '{folder_path}/review-v1.md' and '{folder_path}/todo.md', "
            f"categorizing issues as MUST FIX / SHOULD FIX / CONSIDER.\n\n"
            f"THEN write a machine-readable '{folder_path}/review-v1.json' with EXACTLY this schema "
            f"(valid JSON only, no trailing commas, no comments):\n{REVIEW_JSON_SCHEMA_HINT}\n"
            f"The must_fix array must list every MUST FIX issue as its own object with a stable id "
            f"(MF1, MF2, ...), a precise location, the issue, and a concrete suggestion. "
            f"This array becomes the frozen acceptance checklist for revision, so be exhaustive and specific. "
            f"The needs_citation array must list every sentence that makes a factual claim "
            f"deserving a citation but has no \\\\cite. Suggest a key from the user's bibliography "
            f"only when you are confident it matches; otherwise set suggested_key to null (the "
            f"framework will hand it to the user, never invent a citation).\n\n"
            f"{reference_excerpt}"
        ))
        set_agent_context("Manager")
    if not verify(["review-v1.md", "review-v1.json"]):
        return _finish_with_table(folder_path, results)

    # C3: validate review-v1.json BEFORE entering the convergence loop. A review
    # with an unparseable JSON or a missing/empty must_fix would silently degrade
    # to a single unverified revise pass — the worst kind of "passed". Enforce the
    # schema: must_fix must be a list of {id, location, issue, suggestion}. If it
    # is not, delete the artifacts and force Stage 2 to re-run.
    _review_data = read_json_artifact(folder_path, "review-v1.json")
    _review_must_fix = _review_data.get("must_fix") if isinstance(_review_data, dict) else None
    if not isinstance(_review_must_fix, list) or not all(
        isinstance(it, dict) and it.get("id") and it.get("location")
        for it in _review_must_fix
    ):
        print("[Manager  ] notice     | review-v1.json has an invalid must_fix; forcing Stage 2 re-run", flush=True)
        os.remove(os.path.join(folder_path, "review-v1.json"))
        if os.path.exists(os.path.join(folder_path, "review-v1.md")):
            os.remove(os.path.join(folder_path, "review-v1.md"))
        return _finish_with_table(folder_path, results)  # caller will re-enter; stage2 will run because artifacts removed

    # ── C11: apply citation insertions from the review ──────────────
    # The reviewer flagged needs_citation (sentence + suggested key). A Python
    # pass mechanically inserts \cite{key} at each flagged sentence's end, only
    # for bib-sourced keys from the user's bibliography. Anything without a
    # confident local key is handed to the user (needs_human), never fabricated.
    try:
        from .citation_supplement import apply_insertions
        needs_citation = _review_data.get("needs_citation") if isinstance(_review_data, dict) else None
        insertions = [{"sentence": it.get("sentence", ""),
                       "suggested_key_or_url": it.get("suggested_key") or "",
                       "source": "bib" if it.get("suggested_key") else "none"}
                      for it in (needs_citation or []) if isinstance(it, dict)]
        if insertions:
            draft_path = os.path.join(folder_path, "draft-v1.md")
            if os.path.exists(draft_path):
                text = Path(draft_path).read_text(encoding="utf-8", errors="replace")
                new_text, applied, skipped = apply_insertions(text, insertions)
                if applied:
                    Path(draft_path).write_text(new_text, encoding="utf-8")
                report_lines = [f"- applied \\cite to {applied} sentence(s)",
                                f"- {len(skipped)} left to human:"]
                for s in skipped[:20]:
                    report_lines.append(f"  - {s.get('sentence','')[:80]} ({s.get('reason','')[:40]})")
                Path(os.path.join(folder_path, "citation-insertions.md")).write_text(
                    "\n".join(report_lines) + "\n", encoding="utf-8")
                print(f"[Manager  ] cite-apply | {applied} inserted, {len(skipped)} needs human → {folder_rel}/citation-insertions.md", flush=True)
            else:
                print(f"[Manager  ] notice     | draft-v1.md missing; citation insertions skipped", flush=True)
    except Exception as exc:
        print(f"[Manager  ] notice     | citation insertions skipped: {str(exc)[:160]}", flush=True)

    # ── Stage 3: Revise with convergence loop ───────────────────────
    print(f"\n[Manager  ] Stage 3/4  | Revise (convergence loop, max {MAX_REVISION_ROUNDS} rounds) → {folder_rel}/draft-v2.md", flush=True)
    if os.path.exists(os.path.join(folder_path, "draft-v2.md")):
        print("[Manager  ] skip       | draft-v2.md exists", flush=True)
        results["stage3"] = "skipped"
    else:
        results["stage3"] = run_convergence_loop(
            draft_agent, review_agent, folder_path, folder_rel,
            reference_excerpt, structure,
            run_agent_stage, verify, mode_clause, family,
        )
    if not verify(["draft-v2.md"]):
        return _finish_with_table(folder_path, results)

    # ── Stage 4: Finalize ────────────────────────────────────────────
    print(f"\n[Manager  ] Stage 4/4  | Finalize → {folder_rel}/final.md", flush=True)
    final_outputs = ["final.md", "final.zh.md", "decision.md", "todo.md"]
    if all_exist(final_outputs):
        print("[Manager  ] skip       | final.md/final.zh.md/decision.md/todo.md exist", flush=True)
        results["stage4"] = "skipped"
    else:
        set_agent_context("Review")
        results["stage4"] = run_agent_stage(review_agent, "Review", (
            f"{idea_clause(family)}"
            f"Read draft-v1.md, draft-v2.md, review-v1.md, brief.md, input.md from '{folder_path}'. "
            f"Also read '{structure}'. Do not read references/index.md directly; use the filtered reference excerpt below. "
            f"{mode_clause}"
            f"Merge the best parts, resolve all issues. "
            f"Write '{folder_path}/final.md' (English), '{folder_path}/final.zh.md' (Chinese review), "
            f"'{folder_path}/decision.md', and '{folder_path}/todo.md'.\n\n"
            f"{reference_excerpt}"
        ))
        set_agent_context("Manager")
    all_ok = verify(final_outputs)

    # I2: 在定稿正文上再跑一次数字门禁,覆盖(不是另存)number-check.md。改稿/定稿可能
    # 引入 draft-v1 里没出现的数字,这些也要对回 data/。两次门禁(Stage 1 后 + Stage 4
    # 后)都写同一个 number-check.md:定稿版覆盖初稿版,留下的是最终对账单。任意一处
    # mismatch 都写进 todo.md —— 绝不让一篇定稿悄悄带着与结果库矛盾的数字。
    if PAPER_MODE == "experiment" and all_ok and gate != OFF:
        try:
            from .number_gate import run_number_gate
            final_gate_passed, final_gate_msgs = run_number_gate(
                os.path.join(folder_path, "final.md"), str(DATA_ROOT))
            report = "# Number consistency check (final vs results store)\n\n"
            report += f"Gate level: {gate} (chapter type: {route['type']})\n"
            report += f"Status: {'PASS' if final_gate_passed else 'MISMATCH — see todo.md'}\n\n"
            report += "\n".join(f"- {m}" for m in final_gate_msgs) + "\n"
            Path(os.path.join(folder_path, "number-check.md")).write_text(
                report, encoding="utf-8")
            # Advisory chapters with no store yet: nothing to compare against, so
            # a "no results found" verdict is not a finding worth writing to todo.
            if gate == ADVISORY and not final_gate_passed and any(
                    "no results found" in m or "every number in the draft is unverifiable" in m
                    for m in final_gate_msgs):
                print(f"[Manager  ] number-gate| advisory: no results store; final numbers UNVERIFIED",
                      flush=True)
            elif not final_gate_passed:
                block = "\n## Final number gate: MISMATCHES to fix before submission\n"
                for m in final_gate_msgs:
                    block += f"- {m}\n"
                todo_path = Path(os.path.join(folder_path, "todo.md"))
                existing = todo_path.read_text(encoding="utf-8", errors="replace") if todo_path.exists() else ""
                todo_path.write_text(existing + block, encoding="utf-8")
                print(f"[Manager  ] number-gate| FINAL MISMATCH → appended to {folder_rel}/todo.md",
                      flush=True)
            else:
                print(f"[Manager  ] number-gate| final.md numbers OK → overwrote {folder_rel}/number-check.md",
                      flush=True)
        except Exception as exc:
            print(f"[Manager  ] notice     | final number gate skipped: {str(exc)[:160]}", flush=True)

    # ── Stage 5: 更新跨章状态 ─────────────────────────────────────────
    # cross-chapter-state.md 是章节间传递术语与结论的唯一载体:下一章的 Agent 靠它
    # 拿到"上一章把这个符号定成了什么""哪些结论已经建立"。原先只打印一行"记得手动
    # 更新"——而漏一次,后面章节的术语就开始漂。
    if PAPER_MODE == "experiment" and all_ok:
        xchap_path = os.path.join(str(Path(folder_path).parent), CROSS_CHAPTER_STATE)
        results["stage5_xchap_ok"] = False
        if os.path.exists(xchap_path):
            before_text = Path(xchap_path).read_text(encoding="utf-8", errors="replace")
            candidate_path = os.path.join(folder_path, "cross-chapter-draft.md")
            candidate = Path(candidate_path)
            if candidate.exists():
                candidate.unlink()
            print(f"\n[Manager  ] Stage 5/5  | Update cross-chapter state → "
                  f"{display_path(xchap_path)}", flush=True)
            set_agent_context("Review")
            results["stage5_xchap"] = run_agent_stage(review_agent, "Review", (
                f"Read '{folder_path}/final.md' and '{folder_path}/todo.md', then read "
                f"'{xchap_path}'.\n\n"
                f"Prepare the complete updated contents for '{xchap_path}' so the NEXT "
                f"chapter can stay consistent with this one, but write the result to "
                f"'{candidate_path}' and do not modify '{xchap_path}'. Upsert THIS chapter's "
                f"entries in the three existing sections; do not "
                f"delete other chapters' entries and do not restructure the file. Every "
                f"bullet you write for this chapter MUST begin exactly `- [{chapter}] `, "
                f"and you must replace any existing bullets carrying that same prefix so "
                f"that re-running this stage is idempotent:\n"
                f"- '## Terminology Decisions': every term whose canonical spelling, "
                f"abbreviation expansion, or symbol definition this chapter fixed. Take them "
                f"from the Notation and Terminology Table in draft-v1.plan.md if present.\n"
                f"- '## Per-Chapter Key Claims': ONE sentence naming what this chapter "
                f"established.\n"
                f"- '## Unresolved Cross-Chapter Issues': only items from todo.md that a "
                f"LATER chapter must resolve or coordinate on. Skip anything local to this "
                f"chapter.\n\n"
                f"Keep the three `## ` headings exactly as they are — the pipeline locates "
                f"the sections by those titles.\n\n"
                f"Do NOT restate the contribution or the novelty claim — those live in the "
                f"author's idea.md, which every chapter reads directly; a second copy here "
                f"would drift. Keep the whole file concise: it is injected into later "
                f"chapters' prompts, so every line costs context in every subsequent run.\n\n"
                f"Write the complete updated file to '{candidate_path}' with write_file."
            ))
            set_agent_context("Manager")
            # 验证候选文件,通过了才原子替换。Stage 5 是章节间的唯一交接点:
            # 它静默失败,后面每一章都会各自另立一套术语,而流水线全程显示 DONE。
            # 让它写候选文件而不是直接写目标,是因为"它删掉了前几章的条目"这种失败
            # 只能在写完之后才发现——那时目标文件已经被覆盖,前章的约定找不回来了。
            if not candidate.is_file():
                print(f"[Manager  ] FAIL      | Stage 5 没写出 "
                      f"{display_path(candidate_path)};{CROSS_CHAPTER_STATE} 未更新",
                      flush=True)
                print(f"[Manager  ] FAIL      | 下一章将拿不到本章的术语约定;"
                      f"重跑本章(final.md 已在,只会重跑 Stage 5)", flush=True)
                all_ok = False
            else:
                after_text = candidate.read_text(encoding="utf-8", errors="replace")
                preserved = cross_chapter_state_preserves_others(
                    before_text, after_text, chapter)
                has_claim = cross_chapter_state_has_claim(after_text, chapter)
                results["stage5_xchap_ok"] = preserved and has_claim
                if not preserved:
                    print(f"[Manager  ] FAIL      | Stage 5 改动或删除了其他章节的条目;"
                          f"{CROSS_CHAPTER_STATE} 未更新,候选结果留在 "
                          f"{display_path(candidate_path)}", flush=True)
                elif not has_claim:
                    print(f"[Manager  ] FAIL      | Stage 5 的候选文件没在 "
                          f"'{XCHAP_HEADINGS[1]}' 小节里留下 `- [{chapter}] ` 标记,"
                          f"或破坏了三个 `## ` 标题;{CROSS_CHAPTER_STATE} 未更新",
                          flush=True)
                if results["stage5_xchap_ok"]:
                    os.replace(candidate_path, xchap_path)
                else:
                    print(f"[Manager  ] FAIL      | 下一章将拿不到本章的术语约定;"
                          f"检查 {display_path(candidate_path)} 后重跑本章"
                          f"(final.md 已在,只会重跑 Stage 5)", flush=True)
                    all_ok = False
        else:
            # 不能只是提示。没有跨章状态文件 = 这一章的术语约定无处落盘,后面每一章
            # 都会各自另立一套,而流水线会打印 DONE。
            print(f"[Manager  ] FAIL      | {display_path(xchap_path)} 不存在;"
                  f"本章的术语约定无处落盘", flush=True)
            print(f"[Manager  ] FAIL      | 跑 `python run.py --init` 生成它后重跑本章"
                  f"(只会重跑 Stage 5)", flush=True)
            all_ok = False

    if all_ok:
        print(f"\n[Manager  ] DONE       | All stages complete for {folder_rel}", flush=True)

    # 章节进度汇总表:把 results + 工作区产物整理成一张表,替代"一列一列"的状态行。
    # 放在 return 前,确保无论成功/失败/路由硬停,都能看到一章的完整状态。
    try:
        _print_stage_table(folder_path, results)
    except Exception as exc:
        print(f"[Manager  ] notice     | 进度表格渲染失败(不影响结果): {str(exc)[:120]}", flush=True)
    return results
