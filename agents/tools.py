import os
from pathlib import Path
from smolagents import tool

_current_agent = "Agent"
# 写入作用域:章节流水线运行时设为本章目录,write_file 只允许写进这里。
# inbox 里的作者材料(idea、结果、文献、代码)与 outline 对 Agent 物理只读——
# 仅靠提示词"不要改"约束不住。None = 不限制(--expand / 建索引 / 翻译需要写
# outline 与索引文件,它们用下面的文件白名单约束)。
_write_scope: str | None = None
# 独立阶段(--expand / 建索引 / 翻译大纲)没有"本章目录",改用显式文件白名单:
# 只允许写这几个路径。None = 不启用白名单。
_write_allowlist: set | None = None

# 单次读取上限:超大文件(日志、CSV)整份塞进上下文会撑爆窗口,也会被依赖层静默截断。
MAX_READ_CHARS = 100000
MAX_RANGE_LINES = 400
MAX_LIST_ENTRIES = 200
LOOKUP_LOG_NAME = "fulltext-lookups.md"


def set_agent_context(name: str) -> None:
    global _current_agent
    _current_agent = name


def set_write_scope(root: str | None) -> None:
    """限定 write_file 只能写 root 目录内(含子目录);None 解除限制。"""
    global _write_scope
    _write_scope = os.path.realpath(root) if root else None


def set_write_allowlist(paths) -> None:
    """独立阶段用:write_file 只能写这些文件;None/空 解除。"""
    global _write_allowlist
    _write_allowlist = {os.path.realpath(p) for p in paths} if paths else None


# 程序专属文件:即使在本章目录内,Agent 也不能写(指纹账本、核验状态、brief、数字使用记录)。
PROGRAM_OWNED = {".stage-manifest.json", "verification.md", "brief.md", "numbers-used.json",
                 "fulltext-lookups.md"}


def _outside_write_scope(file_path: str) -> bool:
    if _write_scope is not None and os.path.basename(file_path) in PROGRAM_OWNED:
        return True
    if _write_allowlist is not None:
        return os.path.realpath(file_path) not in _write_allowlist
    if _write_scope is None:
        return False
    # realpath 解析 .. 与符号链接,防止 "chapter/../../idea.md" 之类绕过。
    target = os.path.realpath(file_path)
    try:
        return os.path.commonpath([target, _write_scope]) != _write_scope
    except ValueError:
        # Windows 不同盘符(C: vs D:)无公共路径 → 必然在作用域外,拒绝而不是崩溃。
        return True


def _in_references_room(file_path: str) -> bool:
    """路径在 inbox/references/ 里?原始笔记含作者私人小节(对我的研究的启发等)与
    整篇原文,不许 Agent 直接读;只能走 read_reference / search_reference_text。"""
    import config
    root_real = os.path.realpath(str(config.REFERENCES_ROOT))
    root_abs = os.path.abspath(str(config.REFERENCES_ROOT))
    for target, root in ((os.path.realpath(file_path), root_real),
                         (os.path.abspath(file_path), root_abs)):   # 解析后与未解析的路径都判
        try:
            if os.path.commonpath([target, root]) == root:
                return True
        except ValueError:
            continue
    return False


_REFERENCES_REFUSAL = ("Error: inbox/references/ is not readable directly. Use "
                       "read_reference(key) for the note and search_reference_text(key, query) "
                       "for fulltext excerpts; keys are listed in the reference index.")


def _rel(path: str) -> str:
    return path.replace("\\", "/")


def _log(action: str, path: str) -> None:
    print(f"[{_current_agent:<8}] {action:<10} | {_rel(path)}", flush=True)


@tool
def read_file(file_path: str) -> str:
    """Reads a text file and returns its content (long files are truncated).

    Args:
        file_path: Absolute or relative path to the file to read.
    """
    _log("read_file", file_path)
    if _in_references_room(file_path):
        return _REFERENCES_REFUSAL
    if os.path.basename(file_path).startswith(".env"):
        return "Error: environment files are not readable."
    if not os.path.exists(file_path):
        return f"Error: File '{file_path}' does not exist."
    with open(file_path, "r", encoding="utf-8", errors="replace") as f:
        text = f.read(MAX_READ_CHARS + 1)
    if len(text) > MAX_READ_CHARS:
        return (text[:MAX_READ_CHARS]
                + f"\n\n[TRUNCATED: file longer than {MAX_READ_CHARS} characters. "
                  f"Use read_file_range(file_path, start_line, num_lines) to read the rest. "
                  f"NEVER rewrite this file from a truncated read — you would drop its tail.]")
    return text


@tool
def read_file_range(file_path: str, start_line: int = 1, num_lines: int = 200) -> str:
    """Reads a line range of a text file (1-based start_line), for large files.

    Args:
        file_path: Absolute or relative path to the file to read.
        start_line: First line to return, 1-based.
        num_lines: How many lines to return (at most 400).
    """
    _log("read_range", file_path)
    if _in_references_room(file_path):
        return _REFERENCES_REFUSAL
    if not os.path.exists(file_path):
        return f"Error: File '{file_path}' does not exist."
    start = max(1, int(start_line))
    count = max(1, min(int(num_lines), MAX_RANGE_LINES))
    out, total = [], 0
    with open(file_path, "r", encoding="utf-8", errors="replace") as f:
        for i, line in enumerate(f, start=1):
            total = i
            if start <= i < start + count:
                out.append(line)
    body = "".join(out)
    if len(body) > MAX_READ_CHARS:
        body = body[:MAX_READ_CHARS] + "\n[TRUNCATED: lines too long]"
    return (f"[lines {start}-{start + len(out) - 1} of {total}]\n" + body
            if out else f"[no lines in range; file has {total} lines]")


@tool
def write_file(file_path: str, content: str) -> str:
    """Writes content to a text file, creating parent directories if needed.

    Args:
        file_path: Absolute or relative path to the file to write.
        content: The text content to write to the file.
    """
    if _outside_write_scope(file_path):
        _log("DENIED", file_path)
        return (f"Error: write refused — '{file_path}' is outside what this task may write. "
                f"Author inputs (inbox/, outline) are read-only; write only the "
                f"artifacts your task names.")
    _log("write_file", file_path)
    dir_name = os.path.dirname(file_path)
    if dir_name:
        os.makedirs(dir_name, exist_ok=True)
    with open(file_path, "w", encoding="utf-8") as f:
        f.write(content)
    return f"Successfully written to '{file_path}' ({len(content)} characters)."


@tool
def list_folder(folder_path: str) -> str:
    """Lists files and subdirectories in a folder (long listings are truncated).

    Args:
        folder_path: Path to the folder to list.
    """
    _log("list_folder", folder_path)
    if _in_references_room(folder_path):
        return _REFERENCES_REFUSAL
    if not os.path.isdir(folder_path):
        return f"Error: '{folder_path}' is not a valid directory."
    entries = sorted(os.listdir(folder_path))
    if not entries:
        return "(empty folder)"
    if len(entries) > MAX_LIST_ENTRIES:
        return ("\n".join(entries[:MAX_LIST_ENTRIES])
                + f"\n[TRUNCATED: {len(entries)} entries, showing first {MAX_LIST_ENTRIES}]")
    return "\n".join(entries)


# ── 文献室受控读取 ───────────────────────────────────────────────────────
def _index_dir() -> Path:
    import config
    return Path(config.INDEX_DIR)


def _known_keys() -> set:
    from .references import load_bib
    import config
    return set(load_bib(config.REFS_BIB_PATH)[0])


def _log_lookup(key: str, query: str, paragraphs: list[int]) -> None:
    """把原文查阅记在本章文件夹里,方便作者回头核对模型到底看了哪几段。"""
    if not _write_scope:
        return
    path = Path(_write_scope) / LOOKUP_LOG_NAME
    line = f"- [{key}] query={query!r} paragraphs={paragraphs}\n"
    old = path.read_text(encoding="utf-8") if path.is_file() else "# Fulltext lookups\n\n"
    path.write_text(old + line, encoding="utf-8")


@tool
def read_reference(key: str) -> str:
    """Returns the filtered reading note of one cited paper (by bib key).

    The note is the author's own structured summary of the paper. Prefer it over
    the fulltext. Only keys listed in the reference index exist.

    Args:
        key: The bib key from the reference index, e.g. "he2016resnet".
    """
    _log("read_ref", key)
    from .references import note_view_path
    key = (key or "").strip()
    if key not in _known_keys():
        return f"Error: unknown reference key '{key}'. Use only keys from the reference index."
    path = note_view_path(_index_dir(), key)
    if not path.is_file():
        return f"Reference '{key}' has no reading note; only its bibliographic entry is known. Do not describe its content."
    text = path.read_text(encoding="utf-8", errors="replace")
    if len(text) > MAX_READ_CHARS:
        text = text[:MAX_READ_CHARS] + "\n[TRUNCATED]"
    return text


@tool
def search_reference_text(key: str, query: str) -> str:
    """Returns a few fulltext excerpts of one cited paper that match the query.

    Use this only when the note lacks a detail you need. Excerpts are length-capped;
    every lookup is logged for the author.

    Args:
        key: The bib key from the reference index.
        query: Keywords describing the detail you need (e.g. "residual block depth").
    """
    _log("search_ref", f"{key}: {(query or '')[:60]}")
    import json
    from .references import search_fulltext
    key = (key or "").strip()
    if key not in _known_keys():
        return f"Error: unknown reference key '{key}'. Use only keys from the reference index."
    map_path = _index_dir() / "ref-fulltext.json"
    try:
        mapping = json.loads(map_path.read_text(encoding="utf-8")) if map_path.is_file() else {}
    except (OSError, ValueError):
        mapping = {}
    full = mapping.get(key)
    if not full or not Path(full).is_file():
        return f"Reference '{key}' has no fulltext available; rely on its note."
    text = Path(full).read_text(encoding="utf-8", errors="replace")
    hits = search_fulltext(text, query)
    if not hits:
        return f"No passage of '{key}' matches '{query}'. Try other keywords or rely on the note."
    _log_lookup(key, query, [i for i, _ in hits])
    body = "\n\n".join(f"[paragraph {i}]\n{para}" for i, para in hits)
    return f"# Excerpts from '{key}' for '{query}'\n\n{body}\n"
