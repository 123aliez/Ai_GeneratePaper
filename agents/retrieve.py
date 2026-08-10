"""可选前置步骤 `--retrieve`：按章节检索文献候选，落到候选清单（不污染 bib）。

定位
----
跑在 `--init` 之后、`--all` 之前。读 idea.md + outline.md + data/，为每章生成学术
文献检索 query，调网页检索（``retrieval.search_web``）拿候选，用 ``verify_url`` 过滤
死链/假链，去重后写到 ``references/candidates.md``。

**候选清单不是引用池**：用 ``CAN-xxxx`` 前缀（区别于 bib 的 ``REF-xxxx``），``search_notes``
（``retrieval.py:65``）的 ``REF-`` 过滤不会把它们收进 bib。作者挑中的行手动复制到
``references/bibliography.md``、ID 改 ``REF-xxxx`` 并补 Grade/用途，才进入引用池。

设计原则（与项目"确定性 Python 编排 + 模型只做理解"一致）
--------------------------------------------------------
Manager 只负责**理解三份输入、为每章生成高质量检索 query**；URL 验证、去重、格式化
全部由确定性 Python 执行——防幻觉的关键（``verify_url``）绝不交给模型自由发挥。
"""
import json
import sys
from pathlib import Path

# 与项目其它 agents 模块一致：让项目根可 import（无论作为包还是脚本运行）。
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import (
    IDEA_PATH, DATA_ROOT, WORKSPACE_ROOT, REFERENCES_ROOT,
    RETRIEVAL_MODEL, RETRIEVAL_API_BASE,
)
from .content_source import (
    load_idea_document, idea_is_skeleton,
    load_results_store, list_plots, render_results_summary, data_dir_has_content,
)

# 候选清单产物路径（gitignored 的运行时产物）。
CANDIDATES_PATH = REFERENCES_ROOT / "candidates.md"
# Manager 写出的 per-章 query 中间产物（JSON），供确定性 Python 读回执行检索。
QUERIES_PATH = WORKSPACE_ROOT / "retrieval-queries.json"


def build_retrieval_prompt(chapters: list[dict], idea_text: str,
                           data_summary: str, out_path: str) -> str:
    """Manager 读 idea + 章节 + data 概览，为每章生成 2-4 个文献检索 query。

    Manager 的职责仅限"理解输入 + 生成 query"；执行检索、URL 验证、去重、格式化都由
    确定性 Python 接手。产物是严格 JSON（见下方 schema），写到 ``out_path``。
    """
    chapter_block = _render_chapter_block(chapters)
    return (
        "You are generating ACADEMIC LITERATURE SEARCH QUERIES, one batch per chapter.\n\n"
        f"Read the project's idea below first — it states the contribution and core "
        f"method, so your queries can target the right research area.\n\n"
        f"The paper has {len(chapters)} chapter(s). For EACH chapter, produce 2-4 search "
        f"queries that a literature search engine (Semantic Scholar / Google Scholar / "
        f"arXiv) would resolve to real, citable papers.\n\n"
        "Query design rules:\n"
        "- Each query MUST be a concise English phrase: domain terms + method/technique "
        "names. NOT full sentences. e.g. 'frequency-domain channel attention', not "
        "'papers about channel attention in the frequency domain'.\n"
        "- For idea-family chapters (method/intro/abstract/theory): target the core "
        "method, its direct predecessors, and theoretical foundations.\n"
        "- For data-family chapters (results/experiments/ablation): target comparison "
        "baselines, datasets, and benchmarks the results compare against.\n"
        "- For Related Work: sub-field survey-style queries.\n"
        "- Do NOT invent results or numbers — you only emit search queries, not claims.\n\n"
        f"Write ONLY the file '{out_path}' as STRICT JSON with this exact schema:\n"
        '[\n'
        '  {"chapter": "<folder name from the list below>", '
        '"queries": ["query1", "query2", "query3"]},\n'
        '  ...\n'
        ']\n'
        "The 'chapter' value MUST be exactly the folder name (e.g. '04-method') from "
        "the chapter list below. Emit one object per chapter, in chapter order.\n\n"
        f"=== IDEA ===\n{idea_text}\n\n"
        f"=== DATA OVERVIEW (use for data-family chapter queries) ===\n{data_summary}\n\n"
        f"=== CHAPTERS (folder | type | title | sections) ===\n{chapter_block}\n"
    )


def _render_chapter_block(chapters: list[dict]) -> str:
    """把章节清单渲染成 Manager 可读的紧凑文本（folder/type/title/sections）。"""
    lines = []
    for ch in chapters:
        folder = ch.get("folder", f"{ch.get('number', '?')}-{ch.get('type', '')}")
        title = ch.get("title", "")
        ctype = ch.get("type", "")
        lines.append(f"- [{folder}] type={ctype} | {title}")
        for sec in ch.get("sections", []) or []:
            sec_title = sec.get("title", "")
            bullets = "; ".join(sec.get("bullets", []) or [])
            line = f"    · {sec_title}"
            if bullets:
                line += f" — {bullets}"
            lines.append(line)
    return "\n".join(lines) if lines else "(no chapters)"


def parse_queries(json_path) -> list[tuple[str, list[str]]]:
    """读回 Manager 写出的 queries JSON。

    返回 ``[(chapter_folder, [query, ...]), ...]``。容错：非法 JSON / 非 list / 缺字段 /
    非 list queries → 跳过该条并告警，不整体崩（与项目"宁可降级也不让一步带崩全流程"
    的风格一致）。
    """
    path = Path(json_path)
    if not path.is_file():
        print(f"[retrieve] 警告：{path} 不存在（Manager 没写出）", flush=True)
        return []
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        print(f"[retrieve] 警告：{path} JSON 解析失败：{exc}", flush=True)
        return []
    if not isinstance(raw, list):
        print(f"[retrieve] 警告：{path} 顶层不是数组，跳过", flush=True)
        return []

    out = []
    for i, entry in enumerate(raw):
        if not isinstance(entry, dict):
            print(f"[retrieve] 警告：第 {i} 条不是对象，跳过", flush=True)
            continue
        chapter_value = entry.get("chapter", "")
        if not isinstance(chapter_value, str):
            print(f"[retrieve] 警告：第 {i} 条 chapter 不是字符串，跳过", flush=True)
            continue
        chapter = chapter_value.strip()
        queries = entry.get("queries", [])
        if not isinstance(queries, list):
            print(f"[retrieve] 警告：章 {chapter!r} 的 queries 不是列表，跳过", flush=True)
            continue
        clean = [q.strip() for q in queries if isinstance(q, str) and q.strip()]
        if not chapter or not clean:
            continue
        out.append((chapter, clean))
    return out


def validate_queries(parsed: list[tuple[str, list[str]]], chapters: list[dict]) -> None:
    """确定性校验 Manager 产出的 query 契约：逐章覆盖 outline、顺序一致、每章 2-4 条无重复。

    Manager 可能漏章、重复章、顺序错乱或每章条数失控——这些会让检索覆盖面缺斤短两却
    表面成功。这里用确定性 Python 严校，不合格直接抛 ValueError（由调用方转为报错退出）。
    """
    expected = [ch.get("folder", "") for ch in chapters]
    actual = [chapter for chapter, _queries in parsed]
    if actual != expected:
        raise ValueError(
            f"query 章节须与 outline 完全一致（顺序+集合）；"
            f"expected={expected}, actual={actual}"
        )
    for chapter, queries in parsed:
        if not (2 <= len(queries) <= 4):
            raise ValueError(
                f"{chapter} 应有 2-4 条 query，实际 {len(queries)} 条"
            )
        normalized = [q.casefold() for q in queries]
        if len(set(normalized)) != len(normalized):
            raise ValueError(f"{chapter} 含重复 query")


def _dedupe_and_collect(parsed, max_results_per_query: int = 5):
    """逐 query 调 search_web + verify_url 过滤 + URL 去重。

    返回 ``(hits, stats)``：
      hits    = [{"id", "chapter", "query", "title", "url", "snippet"}]（CAN- 编号）
      stats   = {"queries": N, "raw_hits": N, "verified": N, "filtered": N}
    同一 url 跨章/跨 query 只留一条，但 Chapter 列累加所有来源章（溯源）。
    """
    from .retrieval import search_web
    from .citation_supplement import verify_url

    by_url = {}  # url -> hit dict（Chapter 列在此累积）
    order = []   # 保持首次出现顺序
    raw_hits = 0
    verified = 0
    filtered = 0
    duplicates = 0
    query_count = 0

    for chapter, queries in parsed:
        for q in queries:
            query_count += 1
            hits = search_web(q, max_results=max_results_per_query) or []
            for h in hits:
                raw_hits += 1
                url = str(h.get("url", "")).strip()
                title = str(h.get("title", "")).strip()
                snippet = str(h.get("snippet", "")).strip()
                if not url or not title:
                    filtered += 1
                    continue
                if not verify_url(url):
                    filtered += 1
                    continue
                verified += 1
                if url in by_url:
                    duplicates += 1
                    # 去重：来源章累加，不重复占行。
                    if chapter not in by_url[url]["chapters"]:
                        by_url[url]["chapters"].append(chapter)
                else:
                    by_url[url] = {
                        "title": title, "url": url, "snippet": snippet,
                        "chapters": [chapter], "query": q,
                    }
                    order.append(url)

    hits = []
    for i, url in enumerate(order, start=1):
        rec = by_url[url]
        hits.append({
            "id": f"CAN-{i:04d}",
            "chapter": " ".join(rec["chapters"]),
            "query": rec["query"],
            "title": rec["title"],
            "url": rec["url"],
            "snippet": rec["snippet"],
        })
    stats = {"queries": query_count, "raw_hits": raw_hits,
             "verified": verified, "filtered": filtered, "duplicates": duplicates}
    return hits, stats


def format_candidates(hits: list[dict]) -> str:
    """把去重后的候选渲染成 Markdown 表格（CAN- 前缀，对齐 bib 6 列 + 溯源 2 列）。

    列：ID | Title | Source(Venue/arXiv/DOI) | Authors/Year | Grade | One-line use
        | Chapter | Source Query
    前 6 列与 bib 表头对齐，方便作者挑中后整行复制到 bibliography.md（ID 改 REF-、
    补 Grade 与用途）。后 2 列是检索溯源（复制进 bib 前应删掉）。
    """
    header = (
        "# 候选文献清单（自动检索，URL 已验可达，但内容未经人工核实）\n"
        "# 用法：挑中的行复制到 bibliography.md；ID 从 CAN- 改成 REF-xxxx，\n"
        "# 补 Grade(A-E) 与 One-line use，并删掉 Chapter / Source Query 两列。\n"
        "# 这一列不代表正式引用——search_notes 只收 REF- 前缀，CAN- 不进引用池。\n\n"
        "| ID | Short Title | Authors / Year | Venue / URL | Grade | One-line use | Chapter | Source Query |\n"
        "|---|---|---|---|:---:|---|---|---|\n"
    )
    if not hits:
        return header + "| _(无候选——检索未返回可用结果，检查 RETRIEVAL_MODEL 配置或网络)_ |  |  |  |  |  |  |  |\n"
    rows = []
    for h in hits:
        def _cell(s):
            return str(s).replace("|", "\\|").replace("\r", " ").replace("\n", " ").strip() or " "
        # 列序对齐 bibliography.md：ID | Short Title | Authors/Year(空) | Venue/URL | Grade(空) | use(空) | Chapter | Query
        rows.append(
            f"| {h['id']} | {_cell(h['title'])} |  | {_cell(h['url'])} |  |  | "
            f"{_cell(h['chapter'])} | {_cell(h['query'])} |"
        )
    return header + "\n".join(rows) + "\n"


def run_retrieve(max_results_per_query: int = 5) -> tuple[int, dict]:
    """`--retrieve` 主流程（确定性 Python 编排 + 一次 Manager query 生成）。

    返回 ``(exit_code, stats)``。exit_code: 0=成功写出候选清单；1=前置检查失败或
    Manager 未产出 query。stats 含章数/query 数/命中/过滤计数，供 CLI 打印。
    """
    # ── 前置检查 ──────────────────────────────────────────────────────────
    if not RETRIEVAL_MODEL or not RETRIEVAL_API_BASE:
        print("Error: --retrieve 需要 RETRIEVAL_MODEL + RETRIEVAL_API_BASE（网页检索层）。",
              flush=True)
        print("在 .env 填好这两项（OpenAI-compatible，支持网页搜索的强模型如 grok）再重试。",
              flush=True)
        return 1, {}

    idea_text = load_idea_document()
    if not idea_text:
        print(f"Error: {IDEA_PATH} 不存在或为空。--retrieve 依赖创新点来定位研究领域。",
              flush=True)
        return 1, {}
    is_skel, _words = idea_is_skeleton(idea_text)
    if is_skel:
        print(f"Error: {IDEA_PATH} 还是未填的模板骨架。先填好第 3、4 节（核心洞察+方法设计）。",
              flush=True)
        return 1, {}

    # ── 读 outline + data 概览 ────────────────────────────────────────────
    from agents.outline import parse_outline, OUTLINE_PATH, OutlineRouteError
    try:
        chapters = parse_outline()
    except OutlineRouteError as exc:
        print(f"Error: {exc}", flush=True)
        return 1, {}
    if not chapters:
        print(f"Error: {OUTLINE_PATH} 解析出 0 章。先跑 --init。", flush=True)
        return 1, {}

    if data_dir_has_content():
        data_summary = render_results_summary(load_results_store(), list_plots())
    else:
        data_summary = "(data/ 为空：data 类章节的 query 只能依据 outline 标题生成)"

    # ── Manager 生成 per-章 query ─────────────────────────────────────────
    from agents import create_planner_agent
    from config import get_manager_model
    from agents.orchestrator import run_agent_stage_standalone

    QUERIES_PATH.parent.mkdir(parents=True, exist_ok=True)
    # 防止本轮 Manager 失败时误读上一轮 query（run_agent_stage_standalone 吞异常返回错误，
    # 若不清理旧 JSON，下面会读到陈旧内容继续检索，表面成功实际用的旧 query）。
    try:
        if QUERIES_PATH.exists():
            QUERIES_PATH.unlink()
    except OSError as exc:
        print(f"Error: 无法清理旧 query 文件 {QUERIES_PATH}: {exc}", flush=True)
        return 1, {}

    prompt = build_retrieval_prompt(chapters, idea_text, data_summary, str(QUERIES_PATH))
    print(f"\n检索查询生成：让 Manager 读 idea + {len(chapters)} 章 + data 概览，"
          f"为每章生成检索 query → {QUERIES_PATH.name}...", flush=True)
    stage_result = run_agent_stage_standalone(
        create_planner_agent(get_manager_model()), "Manager", prompt
    )
    if isinstance(stage_result, dict) and stage_result.get("error"):
        print(f"Error: Manager 生成 query 失败：{stage_result['error']}", flush=True)
        return 1, {}

    if not QUERIES_PATH.is_file():
        print(f"Error: Manager 没写出 {QUERIES_PATH}。", flush=True)
        return 1, {}

    parsed = parse_queries(QUERIES_PATH)
    if not parsed:
        print(f"Error: {QUERIES_PATH} 里没有可用 query。", flush=True)
        return 1, {}
    try:
        validate_queries(parsed, chapters)
    except ValueError as exc:
        print(f"Error: Manager query 契约校验失败：{exc}", flush=True)
        print("提示：可重跑 python run.py --retrieve（每次都重新生成 query）。", flush=True)
        return 1, {}

    # ── 确定性 Python：逐 query 检索 + 验 URL + 去重 ───────────────────────
    print(f"\n逐 query 网页检索（每条过 verify_url，URL 去重）...", flush=True)
    hits, stats = _dedupe_and_collect(parsed, max_results_per_query=max_results_per_query)

    # ── 写候选清单（覆盖写）────────────────────────────────────────────────
    REFERENCES_ROOT.mkdir(parents=True, exist_ok=True)
    if CANDIDATES_PATH.exists():
        print(f"将覆盖已有的 {CANDIDATES_PATH.name}（重跑即重新检索）。", flush=True)
    CANDIDATES_PATH.write_text(format_candidates(hits), encoding="utf-8")

    stats["chapters"] = len(parsed)
    stats["candidates"] = len(hits)
    stats["queries_path"] = str(QUERIES_PATH)
    stats["candidates_path"] = str(CANDIDATES_PATH)
    return 0, stats
