import os
from pathlib import Path
from smolagents import tool

_current_agent = "Agent"


def set_agent_context(name: str) -> None:
    global _current_agent
    _current_agent = name


def _rel(path: str) -> str:
    p = path.replace("\\", "/")
    for marker in ("/paper/", "/survey/"):
        if marker in p:
            after = p.split(marker, 1)[1]
            return ("paper/" if marker == "/paper/" else "") + after
    return p


def _log(action: str, path: str) -> None:
    print(f"[{_current_agent:<8}] {action:<10} | {_rel(path)}", flush=True)


def _is_full_reference_index(path: str) -> bool:
    """这个路径是不是那份全量文献索引(不该整份读进上下文)。

    按文件名判断而不是拼死路径:索引在本项目里是 `references/index.md`,而在
    survey 引擎里是 `paper/references/index.md`,两边都要挡住。
    """
    normalized = _rel(path).replace("\\", "/")
    return normalized.endswith("references/index.md")


def _index_path() -> Path:
    """全量文献索引的位置。以 config 为准 —— 原先硬编码 `../../paper/references/`,
    那是 survey 引擎的布局,在本项目里永远指向一个不存在的路径。"""
    from config import REFERENCE_INDEX_PATH
    return Path(REFERENCE_INDEX_PATH)


def _reference_rows():
    """索引里的 `| REF-...` 行;索引不存在时返回空列表。

    本项目默认没有 index.md(引用来自 references/bibliography.md),所以缺文件是
    正常状态而不是错误 —— 让 search_references 返回"0 行"比抛异常有用。
    """
    path = _index_path()
    if not path.is_file():
        return []
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    return [line for line in lines if line.startswith("| REF-")]


@tool
def read_file(file_path: str) -> str:
    """Reads a text file and returns its content.

    Args:
        file_path: Absolute or relative path to the file to read.
    """
    _log("read_file", file_path)
    if _is_full_reference_index(file_path):
        return "Use the injected reference excerpt in the task prompt, or call search_references(query, chapter) for bounded lookup. Do not read the full reference index."
    if not os.path.exists(file_path):
        return f"Error: File '{file_path}' does not exist."
    with open(file_path, "r", encoding="utf-8") as f:
        return f.read()


@tool
def write_file(file_path: str, content: str) -> str:
    """Writes content to a text file, creating parent directories if needed.

    Args:
        file_path: Absolute or relative path to the file to write.
        content: The text content to write to the file.
    """
    _log("write_file", file_path)
    dir_name = os.path.dirname(file_path)
    if dir_name:
        os.makedirs(dir_name, exist_ok=True)
    with open(file_path, "w", encoding="utf-8") as f:
        f.write(content)
    return f"Successfully written to '{file_path}' ({len(content)} characters)."


@tool
def search_references(query: str, chapter: str = "") -> str:
    """Search the global reference index and return up to 20 markdown table rows.

    Args:
        query: Keyword to search in title, tags, key use, or paths.
        chapter: Optional chapter filter such as "03 Alignment".
    """
    q = (query or "").lower()
    chapter = (chapter or "").strip()
    max_rows = 20
    rows = []
    for row in _reference_rows():
        columns = [column.strip() for column in row.strip("|").split("|")]
        if len(columns) < 10:
            continue
        haystack = " ".join(columns).lower()
        chapter_matches = not chapter or columns[4] == chapter or chapter in columns[5]
        query_matches = not q or q in haystack
        if chapter_matches and query_matches:
            rows.append(row)
        if len(rows) >= max_rows:
            break
    if not rows and not _index_path().is_file():
        # 说清是"没有索引"而不是"搜不到" —— 否则 Agent 会反复换关键词重试。
        return (f"# Reference search results\n"
                f"No reference index at {_index_path()}. This project cites from "
                f"references/bibliography.md; use search_literature(query) for "
                f"leads, or work notes-free.\n")

    header = [
        f"# Reference search results",
        f"Query: {query or '(none)'}",
        f"Chapter filter: {chapter or '(none)'}",
        "Rows returned: {count} (max 20)".format(count=len(rows)),
        "",
        "| ID | Title | Year | Evidence | Primary Chapter | Also Relevant To | Paradigm Tags | Key Use | Note Path | Full Text Path |",
        "|---|---|---:|:---:|---|---|---|---|---|---|",
    ]
    return "\n".join(header + rows) + "\n"


# 模块级引用,让测试可以直接赋值 mock(函数内 import 的话补丁打不进去)。
try:
    from .retrieval import two_tier_search
except Exception:  # retrieval 不可用时工具调用会返回错误字符串而非崩溃
    two_tier_search = None


def _format_literature_result(query: str, result) -> str:
    """把 two_tier_search 的返回值渲染成 Agent 可读的 Markdown。

    独立函数便于测试直接传 mock 结果,无需触碰网络或真实检索模块。
    result 不是 dict 时返回错误字符串;字段缺失的条目被防御性过滤,不 KeyError。
    """
    if not isinstance(result, dict):
        return "Error: literature retrieval returned an invalid response."

    raw_notes = result.get("notes", [])
    raw_web   = result.get("web", [])
    notes = [hit for hit in raw_notes
             if isinstance(hit, dict) and str(hit.get("id") or "").strip()
             ] if isinstance(raw_notes, list) else []
    web   = [hit for hit in raw_web
             if isinstance(hit, dict)
             and str(hit.get("title") or "").strip()
             and str(hit.get("url")   or "").strip()
             ] if isinstance(raw_web, list) else []

    lines = [f"# Literature search: {query}", ""]
    lines.append("## Tier 1 — local bibliography / notes (AUTHORITATIVE)")
    if notes:
        lines.append("")
        lines.append("| ID | Title | Note path | Key use |")
        lines.append("|---|---|---|---|")
        for hit in notes:
            lines.append(
                f"| {hit.get('id')} | {hit.get('title') or '(untitled)'} | "
                f"{hit.get('note_path') or '(none)'} | {hit.get('snippet') or ''} |"
            )
        lines.append("")
        lines.append("Cite these with \\cite{<ID>} — they exist in the user's bibliography.")
    else:
        lines.append("")
        lines.append("(no local match — do NOT invent a \\cite key for this claim)")

    # Tier 2 是补充线索,不是可引用来源:web 命中没有本地 bib 条目,插 \cite 会造成
    # 悬空引用,编译前的引用闭合门禁会拦住。所以这里明确标注用途边界。
    lines += ["", "## Tier 2 — web lookup (LEADS ONLY, not citable yet)"]
    if web:
        lines.append("")
        for hit in web:
            lines.append(f"- **{hit.get('title')}** — {hit.get('url')}")
            if hit.get("snippet"):
                lines.append(f"  - {hit.get('snippet')}")
        lines += [
            "",
            "These have NO bibliography entry, so they have no \\cite key. Do not cite "
            "them. Report them in todo.md so the user can add the ones worth keeping.",
        ]
    else:
        lines.append("")
        lines.append("(web tier disabled or no result — work from Tier 1 only)")
    return "\n".join(lines) + "\n"


@tool
def search_literature(query: str, k: int = 5) -> str:
    """Search the local bibliography and (if configured) the web for references.

    Args:
        query: What to look for, e.g. "frequency-domain channel attention".
        k: Maximum number of hits per tier (default 5).
    """
    _log("search_lit", (query or "")[:60])
    # 检索与格式化都包在同一个 try 里:工具函数的返回值会直接进 Agent 的上下文,
    # 抛异常会打断它当前那一步的推理。宁可回一句可读的错误让它继续走,也不要
    # 让一次检索失败把整个起草阶段带崩。
    try:
        search_fn = two_tier_search  # 模块级引用,测试可替换
        if search_fn is None:
            from .retrieval import two_tier_search as _fn
            search_fn = _fn
        result = search_fn(query, k)
        return _format_literature_result(query, result)
    except Exception as exc:
        return f"Error: literature retrieval failed ({exc})."



@tool
def list_folder(folder_path: str) -> str:
    """Lists all files and subdirectories in a folder.

    Args:
        folder_path: Path to the folder to list.
    """
    _log("list_folder", folder_path)
    if not os.path.isdir(folder_path):
        return f"Error: '{folder_path}' is not a valid directory."
    entries = os.listdir(folder_path)
    return "\n".join(entries) if entries else "(empty folder)"
