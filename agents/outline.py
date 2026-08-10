"""Outline → per-chapter workspaces.

`outline.md`(项目根)是使用者**唯一手写的结构文件**:两级标题,`##` 是章、
`###` 是章内小节。本模块把它解析成章节清单,再为每一章生成
`workspace/<NN-type>/brief.md` 与 `input.md`。

为什么 brief 仍然存在(而不是让流水线直接读 outline):

* 上下文分层。Agent 只该看到自己这一章的规格,不该把整篇目录塞进每次调用。
  这与项目既有的"机制文档只喂给它的消费方"是同一条原则。
* 工作区自带规格。`workspace/04-method/` 带着一份 brief,能看出这次运行依据的
  到底是什么规格,不必回溯 outline 的历史版本。
* 既有流水线(orchestrator / chapter_type / prompts)全部读 brief.md,
  换掉它是无收益的大改。

代价是两份文件可能不同步,所以生成的 brief 顶部盖一行 outline 指纹:
outline 改了(类型/小节/要点)却没重新 `--init`,运行时检测到指纹不符会硬停。
"""
import hashlib
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from config import WORKSPACE_ROOT, PROJECT_ROOT
from .chapter_type import normalize_type, route_for_type, DEFAULT_TYPE, DEFAULT_WORDS_BY_TYPE

OUTLINE_PATH = Path(os.getenv("OUTLINE_PATH") or (PROJECT_ROOT / "outline.md"))

# 用户填写的 outline 草稿(表格骨架)。--expand 读它,展开成 outline.md(定稿用)。
# 与 OUTLINE_PATH(outline.md)分离:草稿是用户手写的骨架,定稿是展开/审查后的产物。
OUTLINE_DRAFT_PATH = Path(os.getenv("OUTLINE_DRAFT_PATH")
                          or (PROJECT_ROOT / "outline_draft.md"))

# `## 4. Method` / `## 4 Method` / `## Method`(无号则按出现顺序编号)
_CHAPTER_RE = re.compile(r"^##\s+(?:(\d+)[.、]?\s+)?(.+?)\s*$")
# `### 4.1 总体框架 (~250 words)`;字数段可缺省——没标时按该小节 type 查
# DEFAULT_WORDS_BY_TYPE(小节级优先 → 章 type),都查不到才退回 DEFAULT_SECTION_WORDS
_SECTION_RE = re.compile(
    r"^###\s+(?:[\d.]+\s+)?(.+?)\s*(?:\(~?\s*(\d+)\s*words?\s*\))?\s*$",
    re.IGNORECASE)
# `type: method` / `- type: results` / `类型: 方法`
_TYPE_RE = re.compile(
    r"^\s*(?:[-*+]\s*)?(?:\*\*|__)?\s*(?:type|chapter[ _-]?type|类型|章节类型)\s*"
    r"(?:\*\*|__)?\s*[:：]\s*(.+?)\s*$", re.IGNORECASE)
_BULLET_RE = re.compile(r"^\s*[-*+]\s+(.*\S)\s*$")
_FENCE_RE = re.compile(r"^\s*(```|~~~)")
# ── 表格骨架 ────────────────────────────────────────────────────────────
# 章节骨架的另一种写法:一张表,一行一章。作者只填「章号 | 章标题 | type」,
# 不必碰 `##` / `###` 语法;小节与要点交给 `--expand`。
# 各列按**表头**识别(见 _header_role),不按位置——列序可换、可加备注列。
_TABLE_ROW_RE = re.compile(r"^\s*\|(.+)\|\s*$")
_TABLE_SEP_RE = re.compile(r"^\s*\|[\s:|-]+\|\s*$")

DEFAULT_SECTION_WORDS = 250

BRIEF_FINGERPRINT_PREFIX = "<!-- outline-fingerprint:"
_BRIEF_FINGERPRINT_RE = re.compile(r"^<!--\s*outline-fingerprint:\s*(.*?)\s*-->\s*$")

# ── 写作路由 ─────────────────────────────────────────────────────────
# 章节文件夹一律由 outline.md 经 `--init` 生成,每一章都是整篇里的第 N 章:前后有
# 邻章,术语由 cross-chapter-state.md 跨章传递,开头要接上文、结尾要引下文,已经
# 在前章定义过的符号不能重新定义。不在 outline 里的文件夹不算合法章节(resolve
# 抛 OutlineRouteError,让调用方引导去 --init)。
FULL = "full"

CROSS_CHAPTER_STATE = "cross-chapter-state.md"

# 跨章状态的三个小节标题。Stage 5 按标题定位并追加,orchestrator 跑完后按它们
# 校验文件结构没被破坏——所以这是模块间的契约,不是私有常量。
XCHAP_HEADINGS = (
    "## Terminology Decisions",
    "## Per-Chapter Key Claims",
    "## Unresolved Cross-Chapter Issues",
)


class OutlineRouteError(ValueError):
    """outline 无法安全地作为写作路由来源。"""


class CrossChapterStateError(ValueError):
    """跨章状态结构损坏；拒绝覆盖以避免丢失已积累内容。"""


def _table_cells(line: str) -> list[str]:
    return [cell.strip().strip("`").strip() for cell in line.strip().strip("|").split("|")]


def _header_role(cell: str) -> str:
    """表头单元格 → 它是哪一列("number"/"title"/"type"/"")。"""
    key = cell.strip().strip("*").strip("`").strip().lower()
    if key in {"章号", "序号", "#", "no", "no.", "num", "number"}:
        return "number"
    if key in {"标题", "章节标题", "章标题", "题目", "名称", "章", "章节",
               "title", "chapter", "section"}:
        return "title"
    if key in {"type", "类型", "章节类型"}:
        return "type"
    return ""


def _parse_skeleton_table(text: str) -> list[dict]:
    """从表格骨架解析章节。识别不到就返回 []（调用方回落到标题语法）。

    只认**表头同时含「标题」列与「type」列**的表。这条判据不是形式主义:
    outline 模板里还有一张 `type:` 词表(表头是「写这个 | 主输入 | …」),按
    "第一张表就是骨架"去认会把词表的每一行当成一章,生成十几个空文件夹。
    按表头认列还顺带允许列序随意、允许多余的备注列。
    """
    lines = text.splitlines()
    columns: dict[str, int] | None = None
    chapters: list[dict] = []
    in_fence = False
    auto_number = 0

    for line in lines:
        if _FENCE_RE.match(line):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        if not _TABLE_ROW_RE.match(line):
            if columns is not None:
                break      # 表已结束(空行/正文),不跨表继续收集
            continue
        if _TABLE_SEP_RE.match(line):
            continue

        cells = _table_cells(line)
        if columns is None:
            roles = {}
            for index, cell in enumerate(cells):
                role = _header_role(cell)
                if role and role not in roles:
                    roles[role] = index
            if "title" in roles and "type" in roles:
                columns = roles
            continue

        def cell_at(role: str) -> str:
            index = columns.get(role)
            return cells[index] if index is not None and index < len(cells) else ""

        title = cell_at("title")
        raw_type = cell_at("type")
        # 未替换的占位符行整行跳过。模板发出去时是 `| <章标题> | <type> |`,
        # 使用者可能只填了一半就先跑一次 —— 那时宁可少一章也不要凭空造一章。
        if (not title or not raw_type
                or title.startswith("<") or raw_type.startswith("<")):
            continue

        auto_number += 1
        raw_number = cell_at("number")
        number = int(raw_number) if raw_number.isdigit() else auto_number
        canonical = normalize_type(raw_type)
        chapters.append({
            "number": number, "title": title,
            "type": canonical, "sections": [],
            "unrecognized": raw_type if canonical == DEFAULT_TYPE else "",
        })
    return chapters


def _slug(text: str) -> str:
    """章标题 → 文件夹名可用的短 slug(仅 ASCII 字母数字与连字符)。"""
    ascii_only = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return ascii_only[:24]


def _finalize_chapters(chapters: list[dict]) -> list[dict]:
    """补齐 family/gate/folder 与按 type 的默认字数,两种骨架语法的共用收尾逻辑。"""
    by_folder: dict[str, dict] = {}
    for chapter in chapters:
        if not chapter["type"]:
            guessed = normalize_type(chapter["title"])
            chapter["type"] = guessed if guessed != DEFAULT_TYPE else DEFAULT_TYPE
        chapter["family"], chapter["gate"] = route_for_type(chapter["type"])
        # 没显式标字数的小节,按 type 给经验默认值(小节级 type 优先 → 章 type → 兜底 250)。
        # 显式标过的保持不动(words_explicit=True,不在这里覆盖)。
        for section in chapter["sections"]:
            if not section.get("words_explicit"):
                section_type = section.get("type") or ""
                fallback = DEFAULT_WORDS_BY_TYPE.get(section_type) \
                    or DEFAULT_WORDS_BY_TYPE.get(chapter["type"]) \
                    or DEFAULT_SECTION_WORDS
                section["target_words"] = fallback
        slug = _slug(chapter["title"]) or chapter["type"]
        if chapter["type"] != DEFAULT_TYPE and chapter["type"] not in slug:
            slug = f"{slug}-{chapter['type']}" if slug else chapter["type"]
        chapter["folder"] = f"{chapter['number']:02d}-{slug}".rstrip("-")
        previous = by_folder.get(chapter["folder"])
        if previous is not None:
            raise OutlineRouteError(
                f"outline 中多个章节映射到同一文件夹 '{chapter['folder']}': "
                f"'{previous['title']}' 与 '{chapter['title']}'。")
        by_folder[chapter["folder"]] = chapter
    return chapters


def parse_outline(outline_path=None) -> list[dict]:
    """解析 outline.md,返回章节列表。

    每章::

        {"number": 4, "title": "Method", "type": "method",
         "family": "idea", "gate": "advisory",
         "folder": "04-method",
         "sections": [{"number": 1, "title": "总体框架",
                       "target_words": 250, "type": "",
                       "bullets": ["从 idea.md 展开", ...]}],
         "unrecognized": ""}

    小节的 ``number`` 是**章内序号**(1 起),因为 brief.md 的解析器与分段路由
    都按章内序号工作;outline 里写 `### 4.2` 只是给人看的。
    """
    path = Path(outline_path) if outline_path is not None else OUTLINE_PATH
    if not path.exists():
        return []
    if not path.is_file():
        raise OutlineRouteError(f"outline 路径不是文件: {path}")

    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        raise OutlineRouteError(f"无法读取 outline: {path}: {exc}") from exc

    # 两种语法各管一个阶段:**表格**是作者手写的骨架(只有章标题与 type),
    # **标题语法**是 `--expand` 的产物(`## 章` + `### 小节` + 要点),作者不用手写。
    #
    # 判据是"有没有合法骨架表",不是"有没有 `##` 标题"。后者曾经是判据,但它让
    # 填好的 outline.example.md 完全解析不出章节 —— 模板末尾自带一张 `## type 词表`
    # 供查阅,那个 `##` 会被当成"文件已进入标题语法阶段",于是表格被整个跳过。
    #
    # 反过来不会误判:`--expand` 的产物里没有骨架表(小节要点塞不进单元格),
    # 而 _parse_skeleton_table 要求表头同时有「标题」列与「type」列,type 词表那种
    # 表(表头是「type | 主输入 | 数字门禁」)不满足,自然落到标题语法。
    table_chapters = _parse_skeleton_table(text)
    if table_chapters:
        return _finalize_chapters(table_chapters)

    # ── 标题语法解析 ──────────────────────────────────────────────────────
    chapters: list[dict] = []
    chapter = None
    section = None
    in_fence = False
    auto_number = 0

    for line in text.splitlines():
        if _FENCE_RE.match(line):
            in_fence = not in_fence
            continue
        if in_fence:
            continue  # 代码块里的 `## ...` 是示例语法,不是声明

        chapter_match = _CHAPTER_RE.match(line)
        if chapter_match:
            auto_number += 1
            number = int(chapter_match.group(1)) if chapter_match.group(1) else auto_number
            chapter = {"number": number, "title": chapter_match.group(2).strip(),
                       "type": "", "sections": [], "unrecognized": ""}
            chapters.append(chapter)
            section = None
            continue

        if chapter is None:
            continue  # 首个章节声明之前的说明文字

        section_match = _SECTION_RE.match(line)
        if section_match:
            words = section_match.group(2)
            section = {
                "number": len(chapter["sections"]) + 1,
                "title": section_match.group(1).strip(),
                "target_words": int(words) if words else DEFAULT_SECTION_WORDS,
                # 作者到底写没写字数。缺省会被填成 DEFAULT_SECTION_WORDS,之后就
                # 分不清"没写"与"写了 250";--expand 要序列化回 outline 语法时
                # 必须知道差别,否则会把默认值伪装成作者的决定写进文件。
                "words_explicit": bool(words),
                "type": "", "bullets": [],
            }
            chapter["sections"].append(section)
            continue

        type_match = _TYPE_RE.match(line)
        if type_match:
            raw = type_match.group(1).strip().strip("`").strip()
            if raw.startswith("<") or not raw:
                continue  # 模板占位符 `type: <method|results>`
            canonical = normalize_type(raw)
            if section is not None:
                if canonical != DEFAULT_TYPE:
                    section["type"] = canonical
                continue
            if not chapter["type"]:
                chapter["type"] = canonical
                if canonical == DEFAULT_TYPE:
                    chapter["unrecognized"] = raw
            continue

        bullet_match = _BULLET_RE.match(line)
        if bullet_match and section is not None:
            section["bullets"].append(bullet_match.group(1))

    # 丢掉不是章节的 `##` 标题。outline.md 顶部通常有「写法」「说明」这类小标题,
    # 把它们当成章会生成一个 01-unknown 空文件夹。判据:既没声明 type、标题也推不出
    # 类型、且没有任何 ### 小节——三者同时成立的几乎不可能是真章节。
    # 反过来,`## 1. Abstract` + `type: abstract` 但暂时没写小节的,是合法的章,保留。
    chapters = [c for c in chapters
                if c["type"] or c["sections"]
                or normalize_type(c["title"]) != DEFAULT_TYPE]

    return _finalize_chapters(chapters)


def chapter_fingerprint(chapter: dict) -> str:
    """一章在 outline 里的内容指纹,用于检测 brief 是否已过期。

    覆盖会影响生成结果的一切:类型、小节顺序/标题/字数/覆盖类型/要点。
    要点也算在内——改了要点却不重新 --init,Agent 拿到的还是旧指令。
    """
    parts = [f"type={chapter.get('type')}"]
    for section in chapter.get("sections", []):
        bullets = "|".join(section.get("bullets", []))
        # 使用稳定的 sha256 而不是进程随机化的 hash()——同一内容在不同进程
        # (--init 与实际运行在两次启动中)必须产生相同的指纹才能比对。
        digest = hashlib.sha256(bullets.encode("utf-8")).hexdigest()[:12]
        parts.append(f"{section['number']}:{section['title']}"
                     f":{section['target_words']}"
                     f":{section.get('type') or '-'}"
                     f":{len(bullets)}:{digest}")
    return " ".join(parts)


def read_brief_fingerprint(brief_path) -> str:
    """读出 brief.md 首行的 outline 指纹;无指纹(手写 brief)返回 ""。"""
    path = Path(brief_path)
    if not path.is_file():
        return ""
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            first = handle.readline().strip()
    except OSError:
        return ""
    match = _BRIEF_FINGERPRINT_RE.match(first)
    return match.group(1) if match else ""


def render_brief(chapter: dict, total_chapters: int = 0) -> str:
    """把一章渲染成 orchestrator 能解析的 brief.md。

    小节标题行必须是 `N. **标题** (~字数 words)` —— parse_brief_sections 与
    chapter_type 的正则都认这个形状,格式写错会导致小节解析不出来、分段路由退回
    章级声明。
    """
    lines = [
        f"{BRIEF_FINGERPRINT_PREFIX} {chapter_fingerprint(chapter)} -->",
        f"# {chapter['number']}. {chapter['title']}",
        "",
        "> 本文件由 `outline.md` 生成(`python run.py --init`)。",
        "> **不要手改**:要调整结构请改 outline.md 后重新 --init。",
        "> 本章素材写进同目录的 `input.md`。",
        "",
        "## Type",
        "",
        f"type: {chapter['type']}",
        "",
    ]
    if chapter.get("unrecognized"):
        lines += [f"<!-- outline 里写的 type '{chapter['unrecognized']}' 无法识别,"
                  f"已退回 mixed/advisory -->", ""]
    lines += ["## Section Plan", ""]
    if not chapter["sections"]:
        lines += ["<!-- outline 里这一章没有 ### 小节,流水线会退回单段起草。"
                  "建议在 outline.md 里为它补 ### 小节。 -->", ""]
    for section in chapter["sections"]:
        lines.append(f"{section['number']}. **{section['title']}** "
                     f"(~{section['target_words']} words)")
        if section.get("type"):
            lines.append(f"- type: {section['type']}")
        for bullet in section["bullets"]:
            lines.append(f"- {bullet}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def refresh_cross_chapter_state(old: str, chapters: list[dict]) -> str:
    """刷新顶部的章节顺序清单,同时原样保留三个状态小节里已积累的内容。

    三个标题必须各出现一次且顺序正确。标题被手改、重复或文件被截断时抛
    CrossChapterStateError 拒绝写入——否则 `--force` 会把一个结构损坏的文件
    伪装成"刷新成功",而后面几章的术语对齐依据已经悄悄没了。
    """
    starts = []
    for heading in XCHAP_HEADINGS:
        matches = list(re.finditer(rf"(?m)^{re.escape(heading)}[ \t]*$", old))
        if len(matches) != 1:
            raise CrossChapterStateError(
                f"{CROSS_CHAPTER_STATE} 中标题 '{heading}' 应恰好出现一次,"
                f"实际出现 {len(matches)} 次;文件未修改。")
        starts.append(matches[0].start())
    if starts != sorted(starts):
        raise CrossChapterStateError(
            f"{CROSS_CHAPTER_STATE} 的三个 ## 小节顺序错误;文件未修改。")

    fresh = render_cross_chapter_state(chapters)
    return fresh[:fresh.index(XCHAP_HEADINGS[0])] + old[starts[0]:]


def init_chapter_workspaces(outline_path=None, workspace_root=None,
                            force: bool = False,
                            chapters: list[dict] | None = None) -> dict:
    """按 outline 生成各章工作区。返回 {"created": [...], "skipped": [...], ...}。

    已存在的 brief.md **默认跳过不覆盖**:使用者可能手改过,静默冲掉是不可接受的
    数据丢失。force=True 时才重写(用于 outline 改动后刷新)。指纹不一致的会被
    单独列进 "stale",提示需要 --init --force。
    """
    if chapters is None:
        chapters = parse_outline(outline_path)
    root = Path(workspace_root or WORKSPACE_ROOT)
    result = {"chapters": chapters, "created": [], "skipped": [],
              "stale": [], "updated": [], "cross_chapter": ""}
    if not chapters:
        return result

    # cross-chapter-state.md 由 --init 创建,是跨章术语与结论的载体:Stage 5 往里
    # 追加本章约定,起草提示词读它复用前章定义。所有章节都由 --init 生成,所以这个
    # 文件始终存在。
    root.mkdir(parents=True, exist_ok=True)
    xchap = root / CROSS_CHAPTER_STATE
    if not xchap.exists():
        xchap.write_text(render_cross_chapter_state(chapters), encoding="utf-8")
        result["cross_chapter"] = "created"
    elif force:
        # --force 只刷新顶部的章节顺序清单,三个小节里已积累的内容必须留下:
        # 那是前面几章跑出来的术语约定,冲掉等于让后续章节失去对齐依据。
        # 三个标题必须各出现一次且顺序正确——标题被手改或文件被截断时拒绝写入,
        # 避免 --force 把损坏状态伪装成一次成功刷新。
        old = xchap.read_text(encoding="utf-8", errors="replace")
        xchap.write_text(refresh_cross_chapter_state(old, chapters), encoding="utf-8")
        result["cross_chapter"] = "updated"
    else:
        result["cross_chapter"] = "skipped"

    for chapter in chapters:
        folder = root / chapter["folder"]
        brief_path = folder / "brief.md"
        want = chapter_fingerprint(chapter)

        if brief_path.exists():
            have = read_brief_fingerprint(brief_path)
            if force:
                folder.mkdir(parents=True, exist_ok=True)
                brief_path.write_text(render_brief(chapter), encoding="utf-8")
                result["updated"].append(chapter["folder"])
            elif not have:
                # brief.md 没有 outline 指纹:它不是 --init 生成的(被手改过、或
                # 被人手写后塞进了 outline)。必须显式 --force 换成生成式 brief,
                # 否则会拿着一份没有章序号约定的规格跑整篇路由。
                result["stale"].append(chapter["folder"])
            elif have != want:
                result["stale"].append(chapter["folder"])
            else:
                result["skipped"].append(chapter["folder"])
        else:
            folder.mkdir(parents=True, exist_ok=True)
            brief_path.write_text(render_brief(chapter), encoding="utf-8")
            result["created"].append(chapter["folder"])
    return result


def chapters_without_sections(chapters: list[dict]) -> list[dict]:
    """哪些章一个 `###` 小节都没有。

    这是"表格骨架填完直接 --init"留下的状态:章标题和 type 有了,但没有任何小节
    与起草要点。这种 brief 拿去起草会退回单段、要点为空,Draft 只能照着章标题硬编
    —— 而流水线全程显示成功。所以 --init 必须在这里拦住,提示先跑 --expand。
    """
    return [c for c in chapters if not c["sections"]]


def resolve_write_mode(folder_name: str, outline_path=None) -> dict:
    """解析这一章在 outline 里的位置(整篇路由信息)。

    所有章节文件夹都由 outline.md 经 `--init` 生成,所以一个合法章节**必须**在
    outline 里。三种情况都说明结构文件出问题了,抛 OutlineRouteError 让调用方
    引导去 `--init`,而不是静默退回成另一种写法。

    返回::

        {"mode": "full",
         "position": 4, "total": 7,
         "prev": {...} | None, "next": {...} | None,
         "chapter": {...}}
    """
    path = Path(outline_path) if outline_path is not None else OUTLINE_PATH
    if not path.exists():
        raise OutlineRouteError(
            f"找不到 {path}。所有章节都从它生成,先跑 --init:  python run.py --init"
        )
    chapters = parse_outline(outline_path)
    if not chapters:
        raise OutlineRouteError(
            f"{path} 存在但没有解析出任何章节。"
            "每章需要一个 `## N. 标题` 标题行 + 一行 `type: <类型>`。"
            "修好 outline 后跑:  python run.py --init"
        )
    index = next((i for i, c in enumerate(chapters)
                  if c["folder"] == folder_name), None)
    if index is None:
        raise OutlineRouteError(
            f"'{folder_name}' 不在 {path} 里。所有章节都从 outline 生成,"
            "请先跑:  python run.py --init"
        )
    return {"mode": FULL,
            "position": index + 1, "total": len(chapters),
            "prev": chapters[index - 1] if index > 0 else None,
            "next": chapters[index + 1] if index + 1 < len(chapters) else None,
            "chapter": chapters[index]}


def _section_list(chapter: dict) -> str:
    if not chapter["sections"]:
        return "(no subsections)"
    return ", ".join(s["title"] for s in chapter["sections"])


def build_outline_excerpt(folder_name: str, outline_path=None) -> str:
    """本章 + 前后各一章的**有界**结构摘要,注入 Stage 1a 的规划提示词。

    给规划者邻章视野,它才能划清边界:不把 baseline 对比写进方法章,不把机制解释
    留到结果章。只给标题与小节清单,不给要点正文——这是"有界"的意思:整篇 outline
    不进任何 Agent 的上下文,规划者也只看三章的骨架。
    """
    info = resolve_write_mode(folder_name, outline_path)

    lines = ["PAPER STRUCTURE (bounded excerpt — for boundary awareness only; "
             "do not draft content for the neighbouring chapters):"]
    if info["prev"]:
        prev = info["prev"]
        lines.append(f"  <- {prev['number']}. {prev['title']} "
                     f"[{prev['type']}]: {_section_list(prev)}")
    here = info["chapter"]
    lines.append(f"  >> {here['number']}. {here['title']} "
                 f"[{here['type']}] (THIS CHAPTER): {_section_list(here)}")
    if info["next"]:
        nxt = info["next"]
        lines.append(f"  -> {nxt['number']}. {nxt['title']} "
                     f"[{nxt['type']}]: {_section_list(nxt)}")
    lines.append("Use this only to avoid overlap and to place transitions; "
                 "everything you write must stay inside THIS chapter's scope.")
    return "\n".join(lines) + "\n\n"


# ── 整篇写作契约 ─────────────────────────────────────────────────────
# 这一节定的是「符号在哪定义、过渡句怎么写、术语跟谁对齐」。所有章节都是整篇里
# 的一章,所以只有一套指令。
_FULL_CLAUSE = (
    "WRITING MODE: FULL-PAPER (this is chapter {position} of {total} in "
    "`outline.md`). The chapter is one part of a whole:\n"
    "- Do NOT re-define notation, abbreviations or terms that an EARLIER chapter "
    "already fixed. Take them from `{xchap}` and reuse them verbatim; only "
    "introduce a symbol here if it is genuinely first used in this chapter.\n"
    "- Cross-references to other chapters ARE allowed and expected, but refer to "
    "them by name (\"the method chapter\", \"Section~\\ref{{sec:method}}\"), never "
    "by a guessed number.\n"
    "- {opening} {closing}\n"
    "- Anything that belongs to a neighbouring chapter must be left out, even if "
    "you have the material for it — duplication across chapters is a defect here.\n\n"
)


def build_mode_clause(folder_name: str, outline_path=None,
                      cross_chapter_path: str = "") -> str:
    """写作路由子句:决定符号要不要重定义、过渡句怎么写。

    进 Stage 1a 规划者与 Stage 1b/1c 起草者的提示词。这一章有没有邻章、是首章还是
    末章,只有 outline 解析结果知道。
    """
    info = resolve_write_mode(folder_name, outline_path)
    opening = ("This is the FIRST chapter, so open the paper rather than "
               "continuing from anything."
               if not info["prev"] else
               f"Open by connecting to the preceding chapter "
               f"(\"{info['prev']['title']}\") in at most one sentence; do not "
               f"summarise it.")
    closing = ("This is the LAST chapter, so close the paper rather than handing "
               "off to a following chapter."
               if not info["next"] else
               f"End with a transition that sets up the following chapter "
               f"(\"{info['next']['title']}\") without writing its content.")
    return _FULL_CLAUSE.format(
        position=info["position"], total=info["total"],
        xchap=cross_chapter_path or CROSS_CHAPTER_STATE,
        opening=opening, closing=closing)


_FULL_REVIEW_CLAUSE = (
    "WRITING MODE: FULL-PAPER (chapter {position} of {total}). Judge it as one "
    "part of a whole: raise MUST FIX when it re-defines a term already fixed in "
    "`{xchap}`, when it duplicates material that belongs to a neighbouring "
    "chapter, or when the opening/closing transition is missing. Do NOT ask it to "
    "restate background that an earlier chapter established.\n\n"
)


def build_mode_review_clause(folder_name: str, outline_path=None,
                             cross_chapter_path: str = "") -> str:
    """审稿人视角的路由子句。与起草侧对称。"""
    info = resolve_write_mode(folder_name, outline_path)
    return _FULL_REVIEW_CLAUSE.format(
        position=info["position"], total=info["total"],
        xchap=cross_chapter_path or CROSS_CHAPTER_STATE)


def render_cross_chapter_state(chapters: list[dict]) -> str:
    """`--init` 生成的 cross-chapter-state.md 骨架。

    它是跨章术语与结论的载体:Stage 5 往里追加本章约定,起草提示词读它复用前章
    定义。三个小节的标题被 Stage 5 的提示词按名字追加,改名会让追加失败。
    """
    lines = [
        "# Cross-chapter state",
        "",
        "> 由 `python run.py --init` 生成,之后由每章跑完后的 Stage 5 自动追加。",
        "> 手改也可以(比如提前钉死术语),但**不要改三个 `##` 标题**:流水线按标题追加。",
        "",
        "章节顺序(来自 outline.md):",
    ]
    for chapter in chapters:
        lines.append(f"- {chapter['folder']} — {chapter['number']}. "
                     f"{chapter['title']} [{chapter['type']}]")
    lines += [
        "",
        "## Terminology Decisions",
        "",
        "<!-- 术语/缩写/符号的唯一写法。后面的章节必须照抄,不得另立一套。 -->",
        "",
        "## Per-Chapter Key Claims",
        "",
        "<!-- 每章一句结论,供后续章节引用而不必重读全文。 -->",
        "",
        "## Unresolved Cross-Chapter Issues",
        "",
        "<!-- 需要后续章节解决的遗留问题。 -->",
        "",
    ]
    return "\n".join(lines).rstrip() + "\n"


def outline_banner(result: dict) -> list[str]:
    """--init 的终端汇报行。每章打印解析出的路由,便于当场核对类型是否如预期。"""
    lines = []
    state = result.get("cross_chapter")
    if state:
        lines.append(f"  [{state:<7}] {CROSS_CHAPTER_STATE:<24} "
                     "跨章术语与结论的载体(Stage 5 追加)")
    for chapter in result["chapters"]:
        state = ("created" if chapter["folder"] in result["created"] else
                 "updated" if chapter["folder"] in result["updated"] else
                 "STALE"   if chapter["folder"] in result["stale"] else "skipped")
        lines.append(f"  [{state:<7}] {chapter['folder']:<24} "
                     f"type={chapter['type']:<11} evidence={chapter['family']:<5} "
                     f"gate={chapter['gate']:<8} sections={len(chapter['sections'])}")
        if chapter.get("unrecognized"):
            lines.append(f"            ! outline 里的 type "
                         f"'{chapter['unrecognized']}' 无法识别,已退回 mixed/advisory")
        if not chapter["sections"]:
            lines.append(f"            ! 这一章没有 ### 小节,将退回单段起草")
    return lines
