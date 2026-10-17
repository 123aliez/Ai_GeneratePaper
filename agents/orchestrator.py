import contextlib
import hashlib
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
    DATA_ROOT,
    IDEA_PATH,
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
  "needs_citation": [{"sentence": "the exact sentence needing a citation", "suggested_key": "a key from the reference index, or null"}]
}"""

VERIFY_JSON_SCHEMA_HINT = """{
  "all_resolved": true or false,
  "items": [{"id": "MF1", "resolved": true or false, "note": "how it was resolved, or what is still missing"}]
}"""


def _review_json_valid(data) -> bool:
    """单轮直调产物的落盘前校验:与 C3 的 must_fix schema 同一判据。

    语法合法但缺 must_fix 清单的对象(如 `{}`)不该被当成功落盘,否则
    C3 校验删产物、整轮重跑,单轮省下的 token 又赔回去(Codex 审查 E4)。
    """
    if not isinstance(data, dict):
        return False
    must_fix = data.get("must_fix")
    if not isinstance(must_fix, list):
        return False
    return all(isinstance(it, dict) and it.get("id") and it.get("location")
               for it in must_fix)


def display_path(path: str) -> str:
    normalized = path.replace("\\", "/")
    marker = "/paper/"
    if marker in normalized:
        return "paper/" + normalized.split(marker, 1)[1]
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
    # 只认 JSON 布尔 true:"false"/"true" 字符串、1、null 一律视为未解决——
    # 宽松判真会把模型写成字符串的 "false" 当成已解决,假收敛。
    return [it for it in items if isinstance(it, dict) and it.get("resolved") is not True]


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
                           if isinstance(it, dict) and it.get("resolved") is not True]
    all_resolved = (not missing and not unknown and not dupes and not unresolved_reported)
    return all_resolved, missing, unknown, dupes


def _open_checklist_items(frozen: list, verdict) -> list:
    """冻结清单中仍未通过的条目(按冻结顺序,每条带 id/location/issue/note)。

    覆盖三种情况:verdict 上报但未解决、verdict 根本没报(缺失 id)、verdict
    文件缺失/损坏(None → 全部冻结项未通过)。原先只看"上报且未解决",后两种
    会被静默放过。
    """
    frozen_items = [it for it in frozen if isinstance(it, dict)]
    if not isinstance(verdict, dict) or not isinstance(verdict.get("items"), list):
        return [dict(it, note="verdict missing or unparseable") for it in frozen_items]
    reported = {}
    for it in verdict["items"]:
        if isinstance(it, dict):
            reported.setdefault(it.get("id"), []).append(it)
    open_items = []
    for it in frozen_items:
        hits = reported.get(it.get("id"), [])
        if len(hits) != 1:
            note = "not reported by verifier" if not hits else "reported more than once"
            open_items.append(dict(it, note=note))
        elif hits[0].get("resolved") is not True:
            open_items.append(dict(it, note=hits[0].get("note") or ""))
    return open_items


def _last_verify_round(folder_path: str) -> int:
    """已存在的最大 review-verify.round-N.json 的 N;没有则 0。"""
    rounds = []
    for p in Path(folder_path).glob("review-verify.round-*.json"):
        m = re.fullmatch(r"review-verify\.round-(\d+)\.json", p.name)
        if m:
            rounds.append(int(m.group(1)))
    return max(rounds, default=0)


def checklist_status(folder_path: str) -> list[str]:
    """从已有产物确定性地重算 MUST FIX 验收状态,返回未核验原因(空 = 通过)。

    不依赖本次是否跑了收敛循环:断点续跑跳过 Stage 3 时结论一致。
    """
    review = read_json_artifact(folder_path, "review-v1.json")
    frozen = review.get("must_fix") if isinstance(review, dict) else None
    if not isinstance(frozen, list):
        return ["review-v1.json 不可解析:MUST FIX 清单无法冻结,修订未经逐条核验"]
    if not frozen:
        return []
    last = _last_verify_round(folder_path)
    if last == 0:
        return [f"MUST FIX 清单({len(frozen)} 项)没有任何核验记录"]
    verdict = read_json_artifact(folder_path, f"review-verify.round-{last}.json")
    # 验收判据与收敛循环完全一致(_checklist_verdict:缺失/未知/重复 id 都不算通过);
    # 逐项 open 列表只用来说明原因。
    all_resolved, _missing, unknown, dupes = _checklist_verdict(frozen, verdict)
    if all_resolved:
        return []
    open_items = _open_checklist_items(frozen, verdict)
    detail = []
    if open_items:
        detail.append(f"仍有 {len(open_items)} 项未通过:"
                      + ", ".join(str(it.get("id", "?")) for it in open_items))
    if unknown:
        detail.append("核验结果含清单外的 id:" + ", ".join(str(x) for x in unknown))
    if dupes:
        detail.append("核验结果重复报告:" + ", ".join(str(x) for x in dupes))
    return [f"MUST FIX 验收未通过(第 {last} 轮核验;{';'.join(detail) or '核验结果不合格'})"]


VERIFICATION_FILENAME = "verification.md"


def write_verification(folder_path: str, reasons: list[str]) -> dict:
    """写 verification.md(Python 确定性产物),返回 {"status", "reasons"}。

    "生成完成" ≠ "证据核验通过":final.md 在,不代表数字对得上、MUST FIX 清完了。
    这份文件让 `--all` 汇总表和作者都能看到区别。
    """
    status = "unverified" if reasons else "verified"
    final = Path(folder_path, "final.md")
    sha = hashlib.sha256(final.read_bytes()).hexdigest()[:16] if final.is_file() else ""
    lines = ["# Verification status (program-generated; do not edit)", "",
             f"Status: {status.upper()}", f"Final-SHA: {sha}", ""]
    if reasons:
        lines.append("本章已生成,但以下证据核验未通过,定稿前需人工处理:")
        lines += [f"- {r}" for r in reasons]
    else:
        lines.append("终稿数字门禁与 MUST FIX 验收清单均已通过。")
    Path(os.path.join(folder_path, VERIFICATION_FILENAME)).write_text(
        "\n".join(lines) + "\n", encoding="utf-8")
    return {"status": status, "reasons": list(reasons)}


def read_verification_status(folder_path: str) -> str:
    """读 verification.md 的状态:"verified" / "unverified" / ""(不存在或无法识别)。"""
    path = Path(os.path.join(folder_path, VERIFICATION_FILENAME))
    if not path.is_file():
        return ""
    m = re.search(r"^Status: (VERIFIED|UNVERIFIED)$",
                  path.read_text(encoding="utf-8", errors="replace"), re.MULTILINE)
    return m.group(1).lower() if m else ""


def _escalate_unresolved(folder_path: str, rounds: int, frozen: list) -> None:
    """At the round cap, append every checklist item that did not pass to todo.md and
    decision.md so they surface for human attention instead of looping forever.

    "Did not pass" = reported unresolved, not reported at all, or the verdict file is
    missing/unparseable (then every frozen item is escalated)."""
    verdict = read_json_artifact(folder_path, f"review-verify.round-{rounds}.json")
    open_items = _open_checklist_items(frozen, verdict)
    if not open_items:
        return
    lines = [f"\n## Unresolved after {rounds} revision rounds (needs human attention)"]
    for it in open_items:
        detail = it.get("note") or it.get("issue", "")
        if it.get("issue") and it.get("note") and it.get("note") != it.get("issue"):
            detail = f"{it.get('issue')} — {it.get('note')}"
        lines.append(f"- [{it.get('id', '?')}] {it.get('location', '')}: {detail}")
    block = "\n".join(lines) + "\n"
    for fname in ("todo.md", "decision.md"):
        path = Path(os.path.join(folder_path, fname))
        existing = path.read_text(encoding="utf-8", errors="replace") if path.exists() else ""
        path.write_text(existing + block, encoding="utf-8")


def run_convergence_loop(draft_agent, review_agent, folder_path, folder_rel,
                         reference_excerpt, structure,
                         run_agent_stage, verify, mode_clause: str = "",
                         family: str = MIXED, res_ids_path=None,
                         record_stage=None, skills_clause: str = ""):
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
            f"{idea_clause(family, res_ids_path)}"
            f"Read brief.md, input.md, draft-v1.md, review-v1.md from '{folder_path}'. "
            f"Also read '{structure}'. "
            f"{mode_clause}"
            f"Address ALL 'MUST FIX' items. Write '{folder_path}/draft-v2.md' and '{folder_path}/todo.md'. "
            f"If major rewrite, also write '{folder_path}/decision.md'.\n\n{reference_excerpt}"
            f"{skills_clause}"
        ))
        set_agent_context("Manager")
        return result

    if not frozen:
        print("[Manager  ] converge   | no MUST FIX items; one polish pass for SHOULD FIX", flush=True)
        set_agent_context("Draft")
        result = run_agent_stage(draft_agent, "Draft", (
            f"{idea_clause(family, res_ids_path)}"
            f"Read brief.md, input.md, draft-v1.md, review-v1.md from '{folder_path}'. "
            f"Also read '{structure}'. "
            f"{mode_clause}"
            f"There are no MUST FIX items. Address SHOULD FIX items where supported by evidence, "
            f"then write a clean '{folder_path}/draft-v2.md' and '{folder_path}/todo.md'.\n\n{reference_excerpt}"
            f"{skills_clause}"
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
                f"{idea_clause(family, res_ids_path)}"
                f"Read brief.md, input.md, {prev_draft}, review-v1.md from '{folder_path}'. "
                f"Also read '{structure}'. .\n\n"
                f"{mode_clause}"
                f"Resolve EVERY item in this frozen acceptance checklist (JSON):\n{checklist_text}\n\n"
                f"Make the concrete change at each item's location. Preserve everything already correct — "
                f"do not rewrite unaffected paragraphs. Write the full revised draft to '{folder_path}/{draft_round}'.\n\n"
                f"{reference_excerpt}"
                f"{skills_clause}"
            ))
            set_agent_context("Manager")
            if not verify([draft_round]):
                return last_result
            if record_stage is not None:
                record_stage([draft_round], "revise")

        # -- verify ONLY the frozen checklist --
        print(f"[Manager  ] Round {round_no}/{MAX_REVISION_ROUNDS} | verify → {folder_rel}/{verdict_round}", flush=True)
        if not os.path.exists(os.path.join(folder_path, verdict_round)):
            set_agent_context("Review")
            run_agent_stage(review_agent, "Review", (
                f"Read {draft_round} and brief.md from '{folder_path}'. .\n\n"
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
            if record_stage is not None:
                record_stage([verdict_round], "verify")

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
    _escalate_unresolved(folder_path, MAX_REVISION_ROUNDS, frozen)
    print("[Manager  ] converge   | round cap reached; unresolved items escalated to todo.md/decision.md", flush=True)
    return last_result


ROUTING_CLAUSES = {
    IDEA: (
        "CHAPTER TYPE: {type} — an IDEA chapter. Its subject is the contribution "
        "itself: what is new, why it works, how it is built. Write it from idea.md "
        "(read in full), which is the author's own statement of the novelty. The "
        "the data index / result tables are only supporting evidence — quote at most a "
        "headline number where it motivates the idea, and leave detailed reporting "
        "to the results chapter. Never substitute experimental outcomes for an "
        "explanation of the mechanism, and never invent a contribution the author "
        "did not claim: write [IDEA NEEDED] or [DESIGN DETAIL NEEDED] instead."
    ),
    DATA: (
        "CHAPTER TYPE: {type} — a DATA chapter. Its subject is what the experiments "
        "showed. Every number must come from the data index (RES catalog) (the RES-ID catalog of "
        "data/): cite it as a `{{RES-xxx}}` placeholder copied VERBATIM from the ID "
        "column; write [MISSING DATA] for anything absent. Read idea.md "
        "only to decide which comparisons matter and how to narrate them — do not "
        "re-explain the method design here, it belongs to the method chapter."
    ),
    MIXED: (
        "CHAPTER TYPE: {type} — a MIXED chapter: it must connect the contribution "
        "to the evidence. Draw the claim from idea.md and support it with numbers "
        "from the data index (RES catalog), each cited as a `{{RES-xxx}}` placeholder copied "
        "VERBATIM from the ID column ([MISSING DATA] otherwise). Do not introduce "
        "a new contribution here, and do not restate the full method design."
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
    # 不用 .format():DATA/MIXED 子句里的 {{RES-xxx}} 会被 .format 吃掉一层
    # 花括号变成 {RES-xxx},与 RES_ID_RE 的双括号语法不再匹配(填充器认不出)。
    clause = ROUTING_CLAUSES.get(family, ROUTING_CLAUSES[MIXED]).replace("{type}", label)
    if len({section_types[n] for n in scope}) > 1:
        per_section = ", ".join(f"section {n} is {section_types[n]}" for n in scope)
        clause += (f" This covers several section types ({per_section}); apply the "
                   f"rule above per section rather than uniformly.")
    in_scope_types = {section_types[n] for n in scope} or {route.get("type", "")}
    if "experiments" in in_scope_types:
        clause += code_clause()
    return clause + "\n\n"


def code_clause() -> str:
    """实验设置类章节可参考代码静态索引;其中的细节一律标 [FROM CODE — 需核对]。"""
    import config
    path = Path(config.CODE_INDEX_PATH)
    if not path.is_file():
        return ""
    return (f" The author's experiment code has been statically indexed at '{path}' (parsed, "
            f"never run). You MAY use it for setup details that the settings file does not "
            f"give, but code defaults are NOT proof of what the experiments actually used: "
            f"end every sentence that relies on it with the marker `[FROM CODE — 需核对]`, "
            f"and never take a result number from code.")


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
        "MUST FIX for any result number TYPED as a literal instead of a "
        "`{{RES-xxx}}` placeholder from the data index (RES catalog) (the placeholder IS the "
        "traceability), any placeholder ID that does not exist in the catalog, "
        "missing baselines or "
        "ablations, absent variance/seed information, and claims stronger than the "
        "data supports. Do not ask for more exposition of the method design here."
    ),
    MIXED: (
        "This is a {type} chapter — it must connect the contribution to the "
        "evidence. Raise MUST FIX when a claim about the idea is unsupported by the "
        "results, when a result number is typed literally instead of a "
        "`{{RES-xxx}}` placeholder from the data index (RES catalog), or when the "
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
    family = route.get("family", MIXED)
    # 同上:replace 而非 format,保住 {{RES-xxx}} 的双括号。
    clause = REVIEW_ROUTING_CLAUSES.get(family, REVIEW_ROUTING_CLAUSES[MIXED]).replace(
        "{type}", route.get("type", "unknown"))
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
# input.md 不在此列:它可能含作者手填内容,Stage 0 对它是"存在即跳过、绝不覆盖",
# 但它的出处不是本轮提示词,指纹校验对它没有"重跑就能对齐"的语义(重跑也只会跳过)。
# 硬停它等于把所有带手填 input.md 的工作区永久锁死。
# 收敛循环的轮次产物:orchestrator 对它们也有"存在即跳过"的逻辑,路由变了同样
# 会被静默复用,甚至把旧路由的 round draft 提升为 draft-v2.md。数量不固定,用 glob。
ROUTE_DEPENDENT_ARTIFACT_GLOBS = [
    "draft-v2.round-*.md",
    "review-verify.round-*.json",
]


def stage_model_groups() -> dict[str, str]:
    """{阶段: provider/model}。阶段指纹的模型维;配置读取失败时返回空表(两侧同口径)。"""
    try:
        from config import (DRAFT_PROVIDER, DRAFT_MODEL, REVIEW_PROVIDER,
                            REVIEW_MODEL, MANAGER_PROVIDER, MANAGER_MODEL)
    except Exception:
        return {}
    dg = f"{DRAFT_PROVIDER}/{DRAFT_MODEL}"
    rg = f"{REVIEW_PROVIDER}/{REVIEW_MODEL}"
    mg = f"{MANAGER_PROVIDER}/{MANAGER_MODEL}"
    return {"draft": dg, "evidence": dg, "revise": dg,
            "review": rg, "verify": rg, "final": rg,
            "xchap": rg, "plan": mg, "input": mg,
            "number-gate": "", "citation": ""}


def stage_candidates(folder_path: str) -> list[str]:
    """参与阶段指纹校验的产物名(固定清单 + 收敛轮次 glob)。"""
    names = list(ROUTE_DEPENDENT_ARTIFACTS)
    for pattern in ROUTE_DEPENDENT_ARTIFACT_GLOBS:
        names.extend(p.name for p in sorted(Path(folder_path).glob(pattern)) if p.is_file())
    return names


def stale_chapter_artifacts(folder_path: str, brief_fp: str, family: str,
                            model_groups: dict | None = None,
                            inputs: dict | None = None) -> list[str]:
    """纯函数(不调模型):本章哪些"存在即跳过"产物与当前指纹不符。

    不含 cross-chapter-draft.md:Stage 5 失败会留下未记账的候选文件,它由
    Stage 5 自己的凭据检查处理(陈旧即删掉重跑),放进这里会把重跑永久堵死。
    inputs 缺省时按 family 现算。
    """
    from .skills_registry import stale_stage_artifacts, inputs_fingerprint
    if model_groups is None:
        model_groups = stage_model_groups()
    if inputs is None:
        inputs = inputs_fingerprint(family, IDEA_PATH, DATA_ROOT)
    return stale_stage_artifacts(
        folder_path, stage_candidates(folder_path), brief_fp, model_groups, inputs)


def chapter_staleness(folder_path: str) -> list[str]:
    """给 `--all` 的跳过判断用:已完成的章节是否仍与当前输入/提示词/outline 一致。

    返回人读的原因行;空列表 = 可以安全跳过。任何异常都按"需要重跑"处理(保守),
    重跑时 run_4stage_with_progress 的正式门禁会给出准确的硬停信息。
    """
    try:
        from .skills_registry import inputs_fingerprint, changed_inputs
        folder_path = folder_path.replace("\\", "/")
        chapter = chapter_name_from_folder(folder_path)
        brief_fp = read_brief_fingerprint(os.path.join(folder_path, "brief.md"))
        if not brief_fp:
            return ["brief.md 不是 --init 生成的(缺 outline 指纹)"]
        info = resolve_write_mode(chapter)
        if chapter_fingerprint(info["chapter"]) != brief_fp:
            return ["outline.md 已改动,brief.md 已过期"]
        # 结果库歧义/读不了的文件:单章运行时 ensure_res_ids 会拒绝,跳过路径也要拦。
        # 宽松加载器会丢掉 issues,规范化后的 store 可能完全不变、指纹也不变。
        from .number_gate import load_results_store_report, format_store_issues
        _store, issues = load_results_store_report(str(DATA_ROOT))
        if issues["duplicates"] or issues["unreadable"]:
            return ["结果表有歧义或读不了的文件(RES 目录会拒绝生成):"
                    + format_store_issues(issues).splitlines()[0]]
        route = resolve_run_route(folder_path, parse_brief_sections(folder_path))
        inputs = inputs_fingerprint(route["family"], IDEA_PATH, DATA_ROOT)
        stale = stale_chapter_artifacts(folder_path, brief_fp, route["family"], None, inputs)
        if not stale:
            return []
        changed = changed_inputs(folder_path, stale, inputs)
        why = f"作者输入已变化: {', '.join(changed)}" if changed else \
            "输入/提示词/skill/模型指纹不符"
        return [f"{why};陈旧产物: {', '.join(stale)}"]
    except Exception as exc:
        return [f"无法确认是否陈旧({type(exc).__name__}: {str(exc)[:120]})"]


def handoff_is_current(folder_path: str) -> bool:
    """Stage 5 交接凭据是否与当前指纹相符(cross-chapter-draft.md 存在且已按当前
    brief/输入/模型记账)。

    `--all` 判断"已完成"只看 final.md + 共享状态里的 claim;但改了输入、删掉陈旧
    产物重跑到 Stage 4 后中断时,共享状态里还留着旧 claim,章节会被误判完成而
    永远跳过新的交接。凭据不符 → 让该章重跑(前面产物都在,只会重跑 Stage 5)。
    """
    try:
        from .skills_registry import stale_stage_artifacts, inputs_fingerprint
        folder_path = folder_path.replace("\\", "/")
        if not os.path.isfile(os.path.join(folder_path, "cross-chapter-draft.md")):
            return False
        brief_fp = read_brief_fingerprint(os.path.join(folder_path, "brief.md"))
        if not brief_fp:
            return False
        route = resolve_run_route(folder_path, parse_brief_sections(folder_path))
        if stale_stage_artifacts(
                folder_path, ["cross-chapter-draft.md"], brief_fp, stage_model_groups(),
                inputs_fingerprint(route["family"], IDEA_PATH, DATA_ROOT)):
            return False
        # 内容一致性:只比本章自己的条目(`- [<章名>] ` 开头的行)。不比整份文件——
        # 后续章节交接会改写共享状态的其他部分,整份比对会让每次 --all 都重跑所有
        # 前序章节的 Stage 5。本章条目被改动/删除 → 交接失效,需重跑。
        chapter = chapter_name_from_folder(folder_path)
        xchap = os.path.join(str(Path(folder_path).parent), CROSS_CHAPTER_STATE)
        if not os.path.isfile(xchap):
            return False
        marker = f"- [{chapter}] "
        own = lambda text: sorted(l.strip() for l in text.splitlines()
                                  if l.strip().startswith(marker))
        cand_text = Path(folder_path, "cross-chapter-draft.md").read_text(
            encoding="utf-8", errors="replace")
        state_text = Path(xchap).read_text(encoding="utf-8", errors="replace")
        return bool(own(cand_text)) and own(cand_text) == own(state_text)
    except Exception:
        return False


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


def derived_values(folder_path: str) -> list[float]:
    """本章 numbers-used.json 里程序算出的派生数字(DIFF/REL)的值,数字门禁据此豁免。"""
    try:
        data = json.loads(Path(folder_path, "numbers-used.json").read_text(encoding="utf-8"))
        return [float(u["text"]) for u in data.get("used", []) if u.get("kind") in ("DIFF", "REL")]
    except (OSError, ValueError, KeyError, TypeError):
        return []


def final_integrity_reasons(folder_path: str) -> list[str]:
    """终稿完整性检查(确定性,不调模型):数字缺口 / 未知引用 key / 未知表名。

    每次核验都重扫 final.md 与 final.zh.md,不依赖"这次是否刚做过替换"——否则首次定稿
    留下的 ⟨未知结果⟩ 缺口会在重跑时被洗成"已核验"。手改终稿后重跑也走这里。
    返回未核验原因列表(空 = 无问题)。同时把未知引用 key 追加进 todo.md。
    """
    reasons: list[str] = []
    try:
        from .result_ids import residual_res_problems
        from .citation_supplement import citable_keys, unknown_cite_keys
        from .results_tables import unknown_tables, table_names
        allowed = citable_keys()
        known_tables = set(table_names(DATA_ROOT))
        for fname in ("final.md", "final.zh.md"):
            fp = Path(folder_path, fname)
            if not fp.is_file():
                continue
            txt = fp.read_text(encoding="utf-8", errors="replace")
            for msg in residual_res_problems(txt):
                reasons.append(f"{fname} {msg}")
            bad_tables = unknown_tables(txt, known_tables)
            if bad_tables:
                reasons.append(f"{fname} 的 [TABLE] 占位符引用了结果目录里没有的表: {', '.join(bad_tables)}")
            bad = unknown_cite_keys(txt, allowed)
            if bad:
                reasons.append(f"{fname} 引用了本地文献库里没有的 key: {', '.join(bad)}")
                todo = Path(folder_path, "todo.md")
                old = todo.read_text(encoding="utf-8", errors="replace") if todo.exists() else ""
                line = f"- {fname}: 未知引用 key {', '.join(bad)} —— 补进文献库或删掉该引用\n"
                if line not in old:
                    head = "\n## Unknown citation keys\n" if "## Unknown citation keys" not in old else ""
                    todo.write_text(old + head + line, encoding="utf-8")
    except Exception as exc:
        reasons.append(f"终稿 RES/引用残留扫描出错,未核验: {str(exc)[:120]}")
    return reasons


def reverify_chapter(folder_path: str) -> dict:
    """不调模型,只重新核验一章的终稿(作者手改 final.md 之后用)。写 verification.md。

    复用与流水线相同的确定性检查:完整性扫描 + 终稿数字门禁 + MUST FIX 核验状态。
    没有终稿 → 返回 {"status": "missing"}。
    """
    folder_path = folder_path.replace("\\", "/")
    if not os.path.isfile(os.path.join(folder_path, "final.md")):
        return {"status": "missing", "reasons": ["没有 final.md"]}
    reasons = final_integrity_reasons(folder_path)
    # MUST FIX 验收是针对评审当时那份终稿做的。终稿被手改后,旧的“已解决”结论不再可信:
    # 只要有过 MUST FIX 清单,就要求重新评审(或确认),不能靠确定性检查通过就恢复“已核验”。
    vtext = Path(folder_path, VERIFICATION_FILENAME).read_text(encoding="utf-8", errors="replace") \
        if Path(folder_path, VERIFICATION_FILENAME).is_file() else ""
    old_sha = next((ln.split(":", 1)[1].strip() for ln in vtext.splitlines() if ln.startswith("Final-SHA:")), "")
    cur_sha = hashlib.sha256(Path(folder_path, "final.md").read_bytes()).hexdigest()[:16]
    if old_sha and old_sha != cur_sha and os.path.isfile(os.path.join(folder_path, "review-v1.json")):
        reasons.append("终稿在评审之后被手改:MUST FIX 验收是针对旧文本做的,需要人工确认或重新评审本章")
    if not os.path.isfile(os.path.join(folder_path, "final.zh.md")):
        reasons.append("缺少 final.zh.md(中文审阅版)")
    sections = parse_brief_sections(folder_path)
    route = resolve_run_route(folder_path, sections)
    gate = route["gate"]
    if gate != OFF:
        try:
            from .number_gate import run_number_gate
            passed, msgs = run_number_gate(os.path.join(folder_path, "final.md"), str(DATA_ROOT),
                                           exempt_values=derived_values(folder_path))
            no_store = not passed and any(
                "no results found" in m or "every number in the draft is unverifiable" in m for m in msgs)
            if no_store and gate == ADVISORY:
                reasons.append("数字未核验:尚无实验结果库")
            elif not passed:
                reasons.append(f"终稿数字门禁不符 {len(msgs)} 处(见 number-check.md / todo.md)")
            report = ("# Number consistency check (final vs results store)\n\n"
                      f"Gate level: {gate} (chapter type: {route['type']})\n"
                      f"Status: {'PASS' if passed else 'MISMATCH — see todo.md'}\n\n"
                      + "\n".join(f"- {m}" for m in msgs) + "\n")
            Path(folder_path, "number-check.md").write_text(report, encoding="utf-8")
        except Exception as exc:
            reasons.append(f"终稿数字门禁执行出错,数字未核验: {str(exc)[:120]}")
    try:
        reasons.extend(checklist_status(folder_path))
    except Exception as exc:
        reasons.append(f"MUST FIX 核验状态读取出错: {str(exc)[:120]}")
    return write_verification(folder_path, reasons)


def run_agent_stage_standalone(agent, agent_name: str, prompt: str,
                               attempts: int = 3, allowed_files=None):
    """见 _run_agent_stage_standalone。allowed_files:运行期间 write_file 只能写这些
    路径(--expand / 建索引 / 翻译大纲各自只该写自己的产物);None = 不限制。"""
    from .tools import set_write_allowlist
    set_write_allowlist(allowed_files)
    try:
        return _run_agent_stage_standalone(agent, agent_name, prompt, attempts)
    finally:
        set_write_allowlist(None)


def _run_agent_stage_standalone(agent, agent_name: str, prompt: str,
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
def reference_clause(folder_path: str) -> str:
    """每个 stage 提示词末尾的文献提示:索引位置 + 本章已选文献所在的 input.md。"""
    import config
    return (
        f"\n=== REFERENCE LIBRARY ===\n"
        f"Reference index: '{config.REF_INDEX_PATH}' (table of citable keys; open it with "
        f"read_file_range if you need to look up a key). The references chosen for this "
        f"chapter are listed in '{folder_path}/input.md'. Cite ONLY keys from the index: "
        f"\\\\cite{{key}}. Read a paper's note with read_reference(key); search its fulltext "
        f"with search_reference_text(key, query) only when the note lacks a needed detail.\n")


def res_catalog_path() -> Path:
    from .paths import res_catalog_path as _p
    return _p(DATA_ROOT)


def data_index_path() -> Path:
    from .paths import data_index_path as _p
    return _p(DATA_ROOT)


def build_data_index(res_map: dict) -> Path:
    """程序生成数据索引(不调模型):每张结果表一个 `{{RES-xxx}}=原文` 网格。内容不变不重写。"""
    from .results_tables import collect_tables, render_data_index
    from .paths import settings_path
    tables = collect_tables(DATA_ROOT)
    settings = settings_path(DATA_ROOT)
    text = render_data_index(tables, res_map, settings.name if settings.is_file() else "")
    path = data_index_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    if not (path.is_file() and path.read_text(encoding="utf-8", errors="replace") == text):
        tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, path)
    return path


def idea_clause(family: str = MIXED, res_ids_path=None) -> str:
    """注入每个正文 stage 提示词第一行:先读 idea,数据/混合章再读数据索引。

    RES 占位符范式:结果数字只能逐字复制数据索引里的 `{{RES-xxx}}`,装配时程序替换
    真实值;差值/相对提升写 `{{DIFF:..}}`/`{{REL:..}}` 由程序计算;结果表用
    `[TABLE: ...]` 由程序按 CSV 生成。res_ids_path 为 None(没有结果表)时,
    不许写任何结果数字。
    """
    head = (
        f"FIRST read idea.md in full ({IDEA_PATH}) — the author's own statement of the "
        f"contribution (novelty, mechanism, method design). It is the highest-priority "
        f"input: write the contribution from it verbatim, do not paraphrase or water it "
        f"down, and never add a contribution the author did not make. ")
    if family in (DATA, MIXED):
        index = data_index_path()
        if res_ids_path is not None:
            from .paths import settings_path
            settings = settings_path(DATA_ROOT)
            head += (
                f"THEN read '{index}' — the program-generated data index: one grid per result "
                f"table, every cell shown as `{{{{RES-xxx}}}}=value`. NUMBER RULE (hard): cite any "
                f"result number ONLY as a `{{{{RES-xxx}}}}` placeholder copied VERBATIM from the "
                f"index (exact token, double braces included); never type the numeric value, never "
                f"round or restyle it. Put a unit (%, x, ...) outside the braces, e.g. "
                f"`{{{{RES-014}}}}%`. For the gap or relative gain between two results write "
                f"`{{{{DIFF:RES-a,RES-b}}}}` (a−b) or `{{{{REL:RES-a,RES-b}}}}` ((a−b)/b×100, put the % "
                f"outside) — the program computes it; do not compute it yourself. To show a whole "
                f"result table write ONE line `[TABLE: <table name> | caption: <caption> | note: "
                f"<optional note>]` using a table name from the index — the program renders the "
                f"table from the CSV, so never write table cells by hand. For a result absent "
                f"from the index write [MISSING DATA]; do not guess. An unknown or mistyped ID "
                f"surfaces as a visible gap, never as a fabricated number. "
                f"(The complete ID list is '{res_catalog_path()}'; read it with read_file_range "
                f"only if you must.) ")
            if settings.is_file():
                head += (f"The experimental setup (datasets, hardware, hyper-parameters, seeds) is "
                         f"in '{settings}' — read it before describing the setup; never state a "
                         f"setup detail that is not there. ")
        else:
            head += ("There are NO experiment results in the author's results folder: do not write "
                     "any result number; write [MISSING DATA] where a result belongs. ")
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


def run_4stage_with_progress(draft_agent, review_agent, folder_path: str, manager_agent=None) -> dict:
    """章节流水线入口。运行期间把 Agent 的 write_file 限定在本章目录
    (inbox 里的作者输入物理只读),结束后解除。"""
    from .tools import set_write_scope
    set_write_scope(folder_path)

    try:
        return _run_4stage_with_progress(draft_agent, review_agent, folder_path, manager_agent)
    finally:
        set_write_scope(None)


def _run_4stage_with_progress(draft_agent, review_agent, folder_path: str, manager_agent=None) -> dict:
    """4-stage run with fully Python-controlled, human-readable progress output.

    All smolagents raw streaming is disabled; every visible line is written by this
    function or the shared tool wrappers (read_file / write_file / list_folder).
    """
    folder_path = folder_path.replace("\\", "/")
    folder_rel = display_path(folder_path)
    results = {}
    # 阶段指纹记账的输入(演进点③):experiment 分支会填真实值;survey 恒空,
    # record_stage 见空即跳过。放分支外初始化,避免 mode 分叉处名字未定义。
    brief_fp = ""
    res_ids_path = None
    _model_groups: dict[str, str] = {}
    _inputs_fp: dict = {}
    # 方法论 skill 由编排器确定性注入(进阶段指纹),不再靠 Agent 自助 read_file。
    draft_skills = review_skills = ""

    chapter = chapter_name_from_folder(folder_path)
    # C6: in experiment mode do not inject the survey reference index — the
    # experiment's context source is the results store, not literature rows.
    reference_excerpt = reference_clause(folder_path)

    # Chapter-type routing. Parsed up front because the declared type decides what
    # the prompts point at (idea.md vs data-index.md) and whether the number gate
    # may block. An idea chapter drafted from the results store is the
    # architecture-level failure this routing exists to prevent.
    sections = parse_brief_sections(folder_path)
    route = resolve_run_route(folder_path, sections)
    family, gate = route["family"], route["gate"]

    # 上下文来源:每个 stage 都指向 idea.md(数据章再加数字索引),Agent 自己 read_file。
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
                      f"experiment result tables under {DATA_ROOT}", flush=True)
                print(f"[Manager  ] FAIL      | this chapter's content IS the numbers; there is "
                      f"nothing to report and every number would be unverifiable.", flush=True)
                print(f"[Manager  ] FAIL      | provide result tables under inbox/results/ (see README), "
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

    # ── 演进点②:RES-ID 目录生成/合并(数字占位符的前提) ─────────────
    # 结果库有数值结果时生成 RES 目录与数据索引(已存在则合并:旧 ID 不改号,
    # 新 key 追加)。结果库为空 → res_ids_path=None,提示词退回 data-index
    # 表述,行为与改造前一致(idea 章不因此被阻塞)。
    try:
        from .result_ids import ensure_res_ids
        _res_map, res_ids_path = ensure_res_ids(str(DATA_ROOT), res_catalog_path())
        if res_ids_path is not None:
            print(f"[Manager  ] res-ids    | {display_path(str(res_ids_path))} "
                  f"ready ({len(_res_map)} numeric result(s))", flush=True)
    except Exception as exc:
        # Codex 审查 E2:目录生成失败不能静默继续——数据章的占位符纪律
        # 全靠它,缺席时提示词退回 data-index 表述,模型又回到"手打数字"
        # 的旧世界,产物却混进同一批指纹记账。硬停,与门禁缺席同语义。
        from .number_gate import ResultsStoreError
        if isinstance(exc, ResultsStoreError):
            # 数据歧义清单要完整给作者看(冲突 key + 两处来源),不截断。
            for line in str(exc).splitlines():
                print(f"[Manager  ] FAIL      | {line}", flush=True)
        else:
            print(f"[Manager  ] FAIL      | res-ids catalog 生成失败: "
                  f"{str(exc)[:160]}", flush=True)
        results["route_blocked"] = f"res-ids catalog error: {str(exc)[:120]}"
        return _finish_with_table(folder_path, results)

    # ── 演进点③:阶段指纹缓存。brief 指纹之上再叠 prompt/skill/模型指纹 ──
    # 「文件存在即跳过」只认 brief 指纹——改了 prompts.py / skills/*.md /
    # 模型组之后,旧产物仍被静默复用。跑之前把"存在但指纹不符/未记账"的产物
    # 挑出来硬停(不删文件,语义与 warn_stale_route_artifacts 一致:是否作废
    # 由使用者定)。本次改造前生成的老产物没有 manifest 记账,一律视为陈旧——
    # 宁可重跑,不让旧提示词的产物冒充新跑。
    _model_groups = stage_model_groups()
    try:
        from .skills_registry import inputs_fingerprint, changed_inputs
        _inputs_fp = inputs_fingerprint(family, IDEA_PATH, DATA_ROOT)
        _fingerprinted = stale_chapter_artifacts(
            folder_path, brief_fp, family, _model_groups, _inputs_fp)
        if _fingerprinted:
            _changed = changed_inputs(folder_path, _fingerprinted, _inputs_fp)
            results["route_blocked"] = ("stage fingerprints stale "
                                        "(inputs/prompts/skills/model changed)")
            results["stale_stage_artifacts"] = _fingerprinted
            if _changed:
                print(f"[Manager  ] FAIL      | 作者输入已变化: {', '.join(_changed)}"
                      f" —— 旧产物是按旧输入写的", flush=True)
            print(f"[Manager  ] FAIL      | 下列产物生成时的输入/prompt/skill/模型指纹与当前不符,", flush=True)
            print(f"[Manager  ] FAIL      | 「存在即跳过」会把它们当成本次结果静默复用:", flush=True)
            for name in _fingerprinted:
                print(f"[Manager  ] STALE      |   {folder_rel}/{name}", flush=True)
            print(f"[Manager  ] FAIL      | 备份需要保留的内容后删除上述文件再重跑;", flush=True)
            print(f"[Manager  ] FAIL      | 想沿用某个旧产物,在 {folder_rel}/"
                  f".stage-manifest.json 里给对应文件补一条当前指纹条目"
                  f"(见 skills_registry.stage_fingerprint)即可。", flush=True)
            return _finish_with_table(folder_path, results)
    except Exception as exc:
        # Codex 审查 E2:指纹检查自身抛异常(编程错误/IO 损坏)时静默跳过
        # 等于放弃整层防线。硬停;错误信息引导人工介入。
        print(f"[Manager  ] FAIL      | 阶段指纹检查自身出错(不跳过): "
              f"{str(exc)[:160]}", flush=True)
        results["route_blocked"] = f"stage fingerprint check error: {str(exc)[:120]}"
        return _finish_with_table(folder_path, results)

    from .content_source import content_source_summary
    print(f"[Manager  ] content    | {content_source_summary(family)}", flush=True)
    structure = f"{folder_path}/brief.md"
    routing_clause = build_routing_clause(route)
    from .skills_registry import build_skill_injection
    draft_skills = build_skill_injection("draft")
    review_skills = build_skill_injection("review")

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

    def record_stage(artifacts: list[str], stage: str) -> None:
        """演进点③:产物生成后向 .stage-manifest.json 记指纹。

        brief_fp 为空(survey 模式/异常路径)时跳过——没有 brief 指纹就无从
        计算 stage 指纹,而 stale 检查同样在 experiment 分支内,survey 产物
        不会被校验。模型组按阶段取 _model_groups;缺项(配置读取失败等)
        以空串参与指纹,stale 检查用同一张表,两侧一致。
        """
        if not brief_fp:
            return
        try:
            from .skills_registry import record_stage_fingerprint
            for name in artifacts:
                if os.path.exists(os.path.join(folder_path, name)):
                    record_stage_fingerprint(
                        folder_path, name, brief_fp, stage,
                        _model_groups.get(stage, ""), _inputs_fp)
        except Exception as exc:
            # 记账失败会让"存在但未记账"的产物在下次运行被判陈旧硬停(保守失败,
            # 不会冒充新跑),所以这里 notice 即可,不上抛。
            print(f"[Manager  ] notice     | stage fingerprint record skipped "
                  f"({stage}): {str(exc)[:120]}", flush=True)

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
    # 前置阶段失败即停:备料/证据挖掘没产出就继续起草,等于在缺材料的情况下
    # 花四五次模型调用写一章,而终稿的核验状态还可能不体现这个缺口。
    from .evidence_mining import run_input_material, run_evidence_mining
    try:
        if manager_agent is not None:
            results["stage0_input"] = run_input_material(
                manager_agent, folder_path, chapter, family,
                run_agent_stage, set_agent_context, verify)
    except Exception as exc:
        print(f"[Manager  ] FAIL      | Stage 0 备料出错: {str(exc)[:160]}", flush=True)
        results["stage0_input"] = {"error": str(exc)[:160]}
        return _finish_with_table(folder_path, results)
    if manager_agent is not None:
        if not os.path.isfile(os.path.join(folder_path, "input.md")):
            print(f"[Manager  ] FAIL      | input.md 没有生成,本章停止(流程失败即停)", flush=True)
            return _finish_with_table(folder_path, results)
        # input.md 不进 stale 候选(见 ROUTE_DEPENDENT_ARTIFACTS 注释),
        # 但照常记账:免得将来被加进候选时,已生成的文件全部误报陈旧。
        record_stage(["input.md"], "input")
    print(f"\n[Manager  ] Stage 0/4  | Evidence mining ({family} perspectives) "
          f"→ {folder_rel}/evidence-pack.md", flush=True)
    try:
        results["stage0_evidence"] = run_evidence_mining(
            draft_agent, folder_path, chapter,
            reference_excerpt, run_agent_stage, verify, set_agent_context,
            family=family,
        )
    except Exception as exc:
        print(f"[Manager  ] FAIL      | Stage 0 证据挖掘出错: {str(exc)[:160]}", flush=True)
        results["stage0_evidence"] = {"error": str(exc)[:160]}
        return _finish_with_table(folder_path, results)
    if not os.path.isfile(os.path.join(folder_path, "evidence-pack.md")):
        print(f"[Manager  ] FAIL      | evidence-pack.md 没有生成,本章停止(流程失败即停)", flush=True)
        return _finish_with_table(folder_path, results)
    record_stage(["evidence-pack.md"], "evidence")

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
        if os.path.exists(os.path.join(folder_path, "evidence-pack.md")):
            subject = {IDEA: "the idea and its mechanism", DATA: "the results"}.get(
                family, "the idea and the results")
            evidence_clause = (
                f"Also read '{folder_path}/evidence-pack.md': it contains the hard questions "
                f"each perspective posed about {subject} and the grounded answers. Use its "
                f"grounded answers to pick target claims; treat its '## Open Gaps' as drafting "
                f"caveats, not to be silently ignored. ")
        results["stage1_plan"] = run_agent_stage(planner, planner_name, (
            f"{idea_clause(family, res_ids_path)}"
            f"You are acting only as the planner for Stage 1. "
            f"Read brief.md and input.md from '{folder_path}'. "
            f"Also read '{structure}'. "
            f"{routing_clause}"
            f"{evidence_clause}"
            f"{cross_xchap_hint}"
            f"{mode_clause}"
            f"{outline_clause}"
            f""
            f"Write a concrete construction plan to '{folder_path}/draft-v1.plan.md' for exactly "
            f"{part_count} draft part{'s' if part_count > 1 else ''}. "
            f"Use these Python-fixed part boundaries; do not move sections across parts:\n\n{stage1_plan}\n\n"
            f"Begin the plan with a '## Notation and Terminology Table' that fixes, for the whole chapter: "
            f"the canonical spelling of each key term (pick ONE variant, e.g. 'fine-tuning' not 'finetuning'); "
            f"every abbreviation with its first-use expansion; and any mathematical symbol with its definition. "
            f"All {part_count} part{'s' if part_count > 1 else ''} must obey this table verbatim, "
            f"so keep it explicit and unambiguous. "
            f"For each part, specify target claims, required reference keys, forbidden overlap, transition role, and output file. "
            f"Also write '{folder_path}/todo.md' for known planning gaps. Do not write draft prose.\n\n"
            f"{reference_excerpt}"
        ))
        set_agent_context("Manager")
        if not verify(["draft-v1.plan.md", "todo.md"]):
            return _finish_with_table(folder_path, results)
        record_stage(["draft-v1.plan.md"], "plan")

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
        if os.path.exists(os.path.join(folder_path, "evidence-pack.md")):
            evidence_draft_clause = (
                f"If you need grounded details behind a claim, use read_file on "
                f"'{folder_path}/evidence-pack.md' and read ONLY the Q&A for your part's topics — "
                f"do not inline the whole pack. "
            )
        result = run_agent_stage(draft_agent, "Draft", (
            f"{idea_clause(part_family(part, route), res_ids_path)}"
            f"Read brief.md, input.md, and draft-v1.plan.md from '{folder_path}'. "
            f"{build_routing_clause(route, part_family(part, route), part)}"
            f"{mode_clause}"
            f"{evidence_draft_clause}"
            f""
            f"Obey the 'Notation and Terminology Table' in draft-v1.plan.md verbatim: one canonical spelling per term, "
            f"fixed abbreviation expansions, fixed symbol definitions. Never redefine a term or symbol fixed by an earlier part. "
            f"Write only Part {part['index']} to '{folder_path}/{part['output']}'. "
            f"Part boundary:\n{format_stage1_parts([part])}\n\n"
            f"{prev_context}"
            f"Follow draft-v1.plan.md for reference keys, forbidden overlap, and transitions. "
            f"Do not write other parts and do not duplicate material assigned to other parts.\n\n"
            f"{reference_excerpt}"
            f"{draft_skills}"
        ))
        results["stage1_parts"].append(result)
        set_agent_context("Manager")
        if not verify([part["output"]]):
            return _finish_with_table(folder_path, results)
        record_stage([part["output"]], "draft")

    concatenate_stage1_parts(folder_path, len(stage1_parts))
    print(f"[Manager  ] write_file | {folder_rel}/draft-v1.md", flush=True)
    if not verify(["draft-v1.md", "todo.md"]):
        return _finish_with_table(folder_path, results)
    # draft-v1.md 由 Python 拼接各 part,同一指纹下重拼结果不变,照常记账。
    record_stage(["draft-v1.md"], "draft")

    # ── Deterministic number gate (experiment mode) ─────────────────
    # Cross-check every number in draft-v1 against the results store BEFORE the
    # expensive LLM review. Findings go to number-check.md so the reviewer folds
    # them into its MUST FIX list. Two failure modes:
    #   - MISMATCH (draft contradicts a value in the store): non-blocking, folded
    #     into the review as MUST FIX.
    #   - BLOCKING (no results store / no draft): fail closed — an experiment
    #     paper with no data to verify against is unbuildable, so we abort.
    if gate != OFF:
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
            # number-check.md 是 Python 确定性产物,不含模型输出,记空模型组指纹。
            record_stage(["number-check.md"], "number-gate")
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
    else:
        print(f"[Manager  ] number-gate| off (chapter type: {route['type']} — prose only)", flush=True)

    # ── Stage 2: Review (Markdown + structured JSON checklist) ──────
    print(f"\n[Manager  ] Stage 2/4  | Review → {folder_rel}/review-v1.md (+ review-v1.json)", flush=True)
    if all_exist(["review-v1.md", "review-v1.json"]):
        print("[Manager  ] skip       | review-v1.md/review-v1.json exist", flush=True)
        results["stage2"] = "skipped"
    else:
        set_agent_context("Review")
        number_check_clause = ""
        if os.path.exists(os.path.join(folder_path, "number-check.md")):
            number_check_clause = (
                f"Also read '{folder_path}/number-check.md': it is a deterministic cross-check of "
                f"every number in the draft against the results store. Treat each [NUMBER-MISMATCH] "
                f"line as a MUST FIX item (the prose contradicts the recorded results). "
            )
        # 演进点①(修正版,Codex 审查 E1/E3):review-v1.md(散文评审)先走
        # CodeAgent——它需要 read_file 多轮读草稿;review-v1.json(结构化清单)
        # 再由单轮直调把已成文的评审**转换**成 JSON:单轮模型没有读取循环,
        # 只喂路径等于让它凭空评审;而"评审→JSON"的忠实转换正是单轮擅长的。
        # 转换失败(None)→ 追加一次 CodeAgent 专项调用补写 json,行为不弱于
        # 改造前。模型异常仍上抛(ResilientModel 是唯一重试 owner,这里不兜)。
        _review_json_done = False
        _skip_structured = review_agent is not None and \
            getattr(type(review_agent), "_offline_test_stub", False) is True
        results["stage2"] = run_agent_stage(review_agent, "Review", (
            f"{idea_clause(family, res_ids_path)}"
            f"Read draft-v1.md, brief.md, input.md from '{folder_path}'. "
            f"Also read '{structure}'. "
            f"{build_review_routing_clause(route)}"
            f"{mode_review_clause}"
            f"{number_check_clause}"
            f"Write a detailed review to '{folder_path}/review-v1.md' and '{folder_path}/todo.md', "
            f"categorizing issues as MUST FIX / SHOULD FIX / CONSIDER.\n\n"
            + ("" if not _skip_structured else
               f"THEN write a machine-readable '{folder_path}/review-v1.json' with EXACTLY this schema "
               f"(valid JSON only, no trailing commas, no comments):\n{REVIEW_JSON_SCHEMA_HINT}\n"
               f"The must_fix array must list every MUST FIX issue as its own object with a stable id "
               f"(MF1, MF2, ...), a precise location, the issue, and a concrete suggestion. "
               f"This array becomes the frozen acceptance checklist for revision, so be exhaustive and specific. "
               f"The needs_citation array must list every sentence that makes a factual claim "
               f"deserving a citation but has no \\\\cite. Suggest a key from the user's bibliography "
               f"only when you are confident it matches; otherwise set suggested_key to null (the "
               f"framework will hand it to the user, never invent a citation).\n\n")
            + f"{reference_excerpt}"
            + f"{review_skills}"
        ))
        set_agent_context("Manager")
        if not _skip_structured:
            _md_path = os.path.join(folder_path, "review-v1.md")
            _md_review_text = ""
            if os.path.exists(_md_path):
                _md_review_text = Path(_md_path).read_text(
                    encoding="utf-8", errors="replace")
            if _md_review_text.strip():
                from .structured_call import run_structured_call
                from .prompts import REVIEW_INSTRUCTIONS
                from config import get_review_model
                _review_model = get_review_model()
                _user = (
                    f"{idea_clause(family, res_ids_path)}"
                    f"Below is the full text of a completed human-readable review "
                    f"(review-v1.md) of the draft in '{folder_path}'. Convert it into "
                    f"the machine-readable checklist as JSON with EXACTLY this schema "
                    f"(valid JSON only, no trailing commas, no comments):\n"
                    f"{REVIEW_JSON_SCHEMA_HINT}\n"
                    f"Preserve the review's judgements faithfully: every MUST FIX item "
                    f"becomes one must_fix object (stable id MF1, MF2... in document "
                    f"order, precise location, the issue, a concrete suggestion); "
                    f"SHOULD FIX items map to should_fix, notes to consider. Do NOT "
                    f"invent new issues and do NOT drop any. Output ONLY the JSON "
                    f"object, no prose around it.\n\n"
                    f"=== review-v1.md ===\n{_md_review_text}"
                )
                _data = run_structured_call(
                    _review_model, "Review", REVIEW_INSTRUCTIONS, _user,
                    os.path.join(folder_path, "review-v1.json"),
                    REVIEW_JSON_SCHEMA_HINT,
                    validate=_review_json_valid)
                _review_json_done = _data is not None
            else:
                print("[Manager  ] notice     | review-v1.md missing/empty; "
                      "structured conversion skipped", flush=True)
        if not _review_json_done and not _skip_structured:
            # 转换失败的单次兜底:让 CodeAgent 基于自己刚写的 review-v1.md 补 json。
            # 只兜"解析失败(None)",模型异常已在上面上抛,不吞。
            print("[Manager  ] notice     | structured conversion failed; "
                  "falling back to CodeAgent path for review-v1.json", flush=True)
            set_agent_context("Review")
            run_agent_stage(review_agent, "Review", (
                f"Read your own review '{folder_path}/review-v1.md'. "
                f"Write a machine-readable '{folder_path}/review-v1.json' with EXACTLY "
                f"this schema (valid JSON only, no trailing commas, no comments):\n"
                f"{REVIEW_JSON_SCHEMA_HINT}\n"
                f"The must_fix array must faithfully list every MUST FIX issue from "
                f"review-v1.md as its own object (stable id MF1, MF2..., a precise "
                f"location, the issue, a concrete suggestion); map SHOULD FIX items to "
                f"should_fix and notes to consider. Do not invent or drop issues.\n\n"
                f"{reference_excerpt}"
            ))
            set_agent_context("Manager")
    if not verify(["review-v1.md", "review-v1.json"]):
        return _finish_with_table(folder_path, results)
    record_stage(["review-v1.md", "review-v1.json"], "review")

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
                from .citation_supplement import citable_keys
                new_text, applied, skipped = apply_insertions(
                    text, insertions, allowed_keys=citable_keys())
                if applied:
                    Path(draft_path).write_text(new_text, encoding="utf-8")
                report_lines = [f"- applied \\cite to {applied} sentence(s)",
                                f"- {len(skipped)} left to human:"]
                for s in skipped[:20]:
                    report_lines.append(f"  - {s.get('sentence','')[:80]} ({s.get('reason','')[:40]})")
                Path(os.path.join(folder_path, "citation-insertions.md")).write_text(
                    "\n".join(report_lines) + "\n", encoding="utf-8")
                record_stage(["citation-insertions.md"], "citation")
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
            run_agent_stage, verify, mode_clause, family, res_ids_path,
            record_stage=record_stage, skills_clause=draft_skills,
        )
    if not verify(["draft-v2.md"]):
        return _finish_with_table(folder_path, results)
    # 收敛循环内部的 round 产物由循环自行记账;这里记最终晋升的 draft-v2.md
    # (无论本轮是 skip 还是新跑,文件与当前指纹相符才走到这,直接记)。
    record_stage(["draft-v2.md"], "revise")

    # ── Stage 4: Finalize ────────────────────────────────────────────
    print(f"\n[Manager  ] Stage 4/4  | Finalize → {folder_rel}/final.md", flush=True)
    final_outputs = ["final.md", "final.zh.md", "decision.md", "todo.md"]
    if all_exist(final_outputs):
        print("[Manager  ] skip       | final.md/final.zh.md/decision.md/todo.md exist", flush=True)
        results["stage4"] = "skipped"
    else:
        set_agent_context("Review")
        results["stage4"] = run_agent_stage(review_agent, "Review", (
            f"{idea_clause(family, res_ids_path)}"
            f"Read draft-v2.md, review-v1.md, brief.md, input.md from '{folder_path}'. "
            f"Also read '{structure}'. "
            f"{mode_clause}"
            f"draft-v2.md is the verified revision and the ONLY base text — do not read or "
            f"reintroduce anything from draft-v1.md (issues fixed in revision must stay fixed). "
            f"Finalize it with a light polish only: transitions, terminology consistency, "
            f"grammar. Do NOT change any claim, number, `{{{{RES-xxx}}}}` placeholder, citation, "
            f"[MISSING DATA] / [IDEA NEEDED] marker, or the meaning of any sentence, and do NOT add "
            f"new content. If you notice a remaining problem, leave the text as is and list it in "
            f"todo.md instead of fixing it yourself. "
            f"Write '{folder_path}/final.md' (English), '{folder_path}/final.zh.md' (a faithful "
            f"Chinese rendering of final.md for the author to review, same placeholders and markers), "
            f"'{folder_path}/decision.md' (what was polished), and '{folder_path}/todo.md' "
            f"(remaining issues).\n\n"
            f"{reference_excerpt}"
            f"{review_skills}"
        ))
        set_agent_context("Manager")
    all_ok = verify(final_outputs)
    record_stage(["final.md", "final.zh.md"], "final")

    # 核验状态与 all_ok 分离:all_ok = 产物齐全(决定 Stage 5 是否执行);
    # verification_reasons = 证据核验没通过的原因(决定最后是"核验通过"还是
    # "生成完成但未核验")。允许带显式缺口的初稿,但不允许把它报成全部通过。
    verification_reasons: list[str] = []

    # ── 演进点②:定稿后程序替换 RES 占位符(数字不经模型之手的最后一环) ────
    # final.md / final.zh.md 里的 {{RES-xxx}} 在此替换为 data/ 记录值。未知 ID
    # 显式标 ⟨未知结果 xxx⟩ 并写进 todo.md —— 宁可留下看得见的洞,不编值。
    # 替换后的 final 才是交付物,所以发生在 Stage 4 之后、终稿数字门禁之前
    # (门禁对替换后的真实数字对账,占位符已被剥掉/换掉)。
    if res_ids_path is not None and all_ok:
        # Codex 审查 E2/F1:填充是交付链的必需环节(不填,终稿带着 {{RES-xxx}}
        # 出门;数字门禁又会剥掉占位符显示 PASS——假通过)。因此这里不吃
        # 异常继续:预期内的 I/O 失败直接置 all_ok=False 让本章失败,编程
        # 错误原样上抛。写入用原子替换,中断不会留下截断的 final.md。
        from .result_ids import ensure_res_ids, fill_result_refs
        _final_map, _ = ensure_res_ids(str(DATA_ROOT), res_catalog_path())
        for fname in ("final.md", "final.zh.md"):
            fpath = Path(os.path.join(folder_path, fname))
            if not fpath.is_file():
                continue
            text = fpath.read_text(encoding="utf-8", errors="replace")
            used: list = []
            new_text, unresolved = fill_result_refs(text, _final_map, used)
            used_path = Path(folder_path, "numbers-used.json")
            if fname == "final.md" and (used or not used_path.is_file()):
                # 数字使用记录:溯源表(RES 编号 → CSV 键/行)由它生成。续跑时终稿里已没有
                # 占位符(used 为空),不能拿空列表覆盖首次留下的记录。
                used_path.write_text(
                    json.dumps({"chapter": chapter, "used": used}, ensure_ascii=False, indent=1),
                    encoding="utf-8")
                _neg = [u for u in used if u.get("negative")]
                if _neg:
                    _todo = Path(folder_path, "todo.md")
                    _old = _todo.read_text(encoding="utf-8", errors="replace") if _todo.exists() else ""
                    _todo.write_text(_old + "\n## Negative derived numbers (check the wording)\n" + "".join(
                        f"- {u['placeholder']} = {u['text']}: 正文把它写成“提升”还是“下降”?\n" for u in _neg),
                        encoding="utf-8")
            if unresolved:
                block = f"\n## Unresolved RES placeholders in {fname}\n"
                for rid in sorted(set(unresolved)):
                    block += f"- {rid} — no such ID in the data index; fix the placeholder or add the result to inbox/results/\n"
                todo_path = Path(os.path.join(folder_path, "todo.md"))
                existing = todo_path.read_text(encoding="utf-8", errors="replace") if todo_path.exists() else ""
                todo_path.write_text(existing + block, encoding="utf-8")
                # 缺口不算流程失败:章照常交接、继续写后面的章,但标“未核验”并进待补清单。
                print(f"[Manager  ] GAP       | {folder_rel}/{fname} 有 {len(set(unresolved))} 个"
                      f"未解析 RES 占位符 → 已记入 todo.md(本章标为未核验)", flush=True)
                verification_reasons.append(
                    f"{fname} 有 {len(set(unresolved))} 个未解析 RES 占位符(见 todo.md)")
            if new_text != text:
                tmp = fpath.with_suffix(fpath.suffix + ".tmp")
                tmp.write_text(new_text, encoding="utf-8")
                os.replace(tmp, fpath)
        # 交付物里不允许再出现占位符语法(填充后残留 = 上面置了 all_ok=False,
        # 或填充器有 bug)。
        for fname in ("final.md", "final.zh.md"):
            fpath = Path(os.path.join(folder_path, fname))
            if fpath.is_file() and "{{RES-" in fpath.read_text(encoding="utf-8", errors="replace"):
                print(f"[Manager  ] FAIL      | {folder_rel}/{fname} 仍含未处理 "
                      f"RES 占位符语法,不得作为交付物", flush=True)
                all_ok = False
        if all_ok:
            print(f"[Manager  ] res-fill   | placeholders substituted in final.md/final.zh.md "
                  f"from {display_path(str(res_ids_path))}", flush=True)

    # I2: 在定稿正文上再跑一次数字门禁,覆盖(不是另存)number-check.md。改稿/定稿可能
    # 引入 draft-v1 里没出现的数字,这些也要对回 data/。两次门禁(Stage 1 后 + Stage 4
    # 后)都写同一个 number-check.md:定稿版覆盖初稿版,留下的是最终对账单。任意一处
    # mismatch 都写进 todo.md —— 绝不让一篇定稿悄悄带着与结果库矛盾的数字。
    final_gate_attempted = False
    if all_ok and gate != OFF:
        final_gate_attempted = True
        try:
            from .number_gate import run_number_gate
            final_gate_passed, final_gate_msgs = run_number_gate(
                os.path.join(folder_path, "final.md"), str(DATA_ROOT),
                exempt_values=derived_values(folder_path))
            report = "# Number consistency check (final vs results store)\n\n"
            report += f"Gate level: {gate} (chapter type: {route['type']})\n"
            report += f"Status: {'PASS' if final_gate_passed else 'MISMATCH — see todo.md'}\n\n"
            report += "\n".join(f"- {m}" for m in final_gate_msgs) + "\n"
            Path(os.path.join(folder_path, "number-check.md")).write_text(
                report, encoding="utf-8")
            # 终稿门禁覆盖初稿版 number-check.md 后刷新记账(指纹不变,幂等)。
            record_stage(["number-check.md"], "number-gate")
            # Advisory chapters with no store yet: nothing to compare against, so
            # a "no results found" verdict is not a finding worth writing to todo.
            if gate == ADVISORY and not final_gate_passed and any(
                    "no results found" in m or "every number in the draft is unverifiable" in m
                    for m in final_gate_msgs):
                print(f"[Manager  ] number-gate| advisory: no results store; final numbers UNVERIFIED",
                      flush=True)
                verification_reasons.append("数字未核验:尚无实验结果库(data/ 为空)")
            elif not final_gate_passed:
                verification_reasons.append(
                    f"终稿数字门禁不符 {len(final_gate_msgs)} 处(见 number-check.md / todo.md)")
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
            print(f"[Manager  ] FAIL      | final number gate error: {str(exc)[:160]}", flush=True)
            verification_reasons.append(f"终稿数字门禁执行出错,数字未核验: {str(exc)[:120]}")

    if os.path.exists(os.path.join(folder_path, "final.md")):
        if gate != OFF and not final_gate_attempted:
            # 终稿产物不全(或 RES 填充失败)时门禁不会执行——没执行不等于通过。
            verification_reasons.append("终稿产物不全或前置步骤失败,RES 填充/终稿数字门禁未执行")
        # 每次运行都重扫终稿(含 Stage 4 被 skip 的续跑):RES 缺口与未知引用 key
        # 不能因为"本次没重新替换"就从核验状态里消失。
        verification_reasons.extend(final_integrity_reasons(folder_path))
        try:
            verification_reasons.extend(checklist_status(folder_path))
        except Exception as exc:
            verification_reasons.append(f"MUST FIX 核验状态读取出错: {str(exc)[:120]}")
        results["verification"] = write_verification(folder_path, verification_reasons)

    # ── Stage 5: 更新跨章状态 ─────────────────────────────────────────
    # cross-chapter-state.md 是章节间传递术语与结论的唯一载体:下一章的 Agent 靠它
    # 拿到"上一章把这个符号定成了什么""哪些结论已经建立"。原先只打印一行"记得手动
    # 更新"——而漏一次,后面章节的术语就开始漂。
    if all_ok:
        xchap_path = os.path.join(str(Path(folder_path).parent), CROSS_CHAPTER_STATE)
        results["stage5_xchap_ok"] = False
        if os.path.exists(xchap_path):
            before_text = Path(xchap_path).read_text(encoding="utf-8", errors="replace")
            candidate_path = os.path.join(folder_path, "cross-chapter-draft.md")
            candidate = Path(candidate_path)
            # Codex 审查 D3:同指纹 resume 不应重跑 Stage 5。交接完成的凭据 =
            # manifest 里 cross-chapter-draft.md(成功晋升后才记账)指纹相符,
            # 且共享状态文件未被其后手改(比对内容)。凭据在 → skip,像其它
            # "存在即跳过"产物一样。
            from .skills_registry import stale_stage_artifacts
            _xchap_stale = stale_stage_artifacts(
                folder_path, ["cross-chapter-draft.md"], brief_fp,
                {**_model_groups, "xchap": _model_groups.get("xchap", "")},
                _inputs_fp)
            _xchap_recorded = (candidate.is_file() and not _xchap_stale
                               and cross_chapter_state_has_claim(
                                   before_text, chapter))
            if _xchap_recorded and candidate.exists() and \
                    candidate.read_text(encoding="utf-8", errors="replace") == before_text:
                print("[Manager  ] skip       | cross-chapter handoff already recorded "
                      "for current fingerprint", flush=True)
                results["stage5_xchap_ok"] = True
            else:
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
                        # 候选通过了校验才晋升;失败的候选留在盘上,不记指纹
                        # (下次重跑 Stage 5 会先 unlink 它)。晋升后文件被移走,
                        # 先把候选内容记账再移动:凭据 = 内容指纹,不是文件在场
                        # (Codex 审查 D3:os.replace 之后 record 落空)。
                        _handoff_text = Path(xchap_path).read_text(
                            encoding="utf-8", errors="replace")
                        Path(candidate_path).write_text(_handoff_text, encoding="utf-8")
                        record_stage(["cross-chapter-draft.md"], "xchap")
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
        _ver = results.get("verification") or {}
        if _ver.get("status") == "unverified":
            print(f"\n[Manager  ] DONE       | {folder_rel} 生成完成,但未通过核验"
                  f"({len(_ver['reasons'])} 项,见 verification.md):", flush=True)
            for reason in _ver["reasons"]:
                print(f"[Manager  ] UNVERIFIED |   {reason}", flush=True)
        else:
            print(f"\n[Manager  ] DONE       | All stages complete for {folder_rel} "
                  f"(核验通过)", flush=True)

    # 章节进度汇总表:把 results + 工作区产物整理成一张表,替代"一列一列"的状态行。
    # 放在 return 前,确保无论成功/失败/路由硬停,都能看到一章的完整状态。
    try:
        _print_stage_table(folder_path, results)
    except Exception as exc:
        print(f"[Manager  ] notice     | 进度表格渲染失败(不影响结果): {str(exc)[:120]}", flush=True)
    return results
