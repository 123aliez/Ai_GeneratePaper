"""脚本侧的材料供给:替代原来让模型自己调用的 read_reference / search_reference_text。

- 本章用到哪些文献:取 input.md 里出现的、refs.bib 中存在的 key(备料阶段把选中的文献写进
  input.md),脚本把这些文献的笔记(已过滤掉"对我的研究的启发"等私人小节)作为文档附进提示词;
- 模型需要笔记里没有的细节时,在回复里写 <lookup key="键">查询词</lookup>,由 lookup_fulltext 代查,
  结果受长度限制,并记入本章的 fulltext-lookups.md,作者可回头核对模型到底看了哪几段。
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from .content_stage import Doc

LOOKUP_LOG_NAME = "fulltext-lookups.md"
NOTE_CHARS = 7000
MAX_NOTES = 14
# 全库笔记总量不超过这个数时,备料阶段直接附上全部笔记,省掉一轮"先选文献"
ALL_NOTES_BUDGET = 60_000


def _config():
    import config
    return config


def known_keys() -> set[str]:
    from .references import load_bib
    return set(load_bib(_config().REFS_BIB_PATH)[0])


def note_text(key: str, cap: int = NOTE_CHARS) -> str | None:
    """某篇文献过滤后的笔记;没有笔记返回 None。"""
    from .references import note_view_path
    path = note_view_path(Path(_config().INDEX_DIR), key)
    if not path.is_file():
        return None
    text = path.read_text(encoding="utf-8", errors="replace")
    return text if len(text) <= cap else text[:cap] + "\n[TRUNCATED]"


def keys_in_text(text: str, valid: set[str] | None = None) -> list[str]:
    """文本里出现的、确实是 refs.bib 键的词,按首次出现的顺序去重。"""
    valid = known_keys() if valid is None else valid
    seen: list[str] = []
    for token in re.findall(r"[A-Za-z][A-Za-z0-9_:.\-]*", text or ""):
        token = token.rstrip(".:-")
        if token in valid and token not in seen:
            seen.append(token)
    return seen


def note_docs(keys: list[str], max_notes: int = MAX_NOTES) -> list[Doc]:
    """把这些文献的笔记做成文档。没有笔记的文献给一条说明,避免模型凭标题编内容。"""
    docs = []
    for key in keys[:max_notes]:
        text = note_text(key)
        if text is None:
            docs.append(Doc(f"note:{key}", text="(This paper has no reading note; only its "
                                              "bibliographic entry is known. Do not describe its content.)"))
        else:
            docs.append(Doc(f"note:{key}", text=text))
    return docs


def all_notes_size() -> int:
    total = 0
    for key in known_keys():
        text = note_text(key)
        total += len(text or "")
    return total


def chapter_note_docs(folder_path) -> list[Doc]:
    """本章要附的文献笔记:input.md 里提到的那些文献。"""
    input_md = Path(folder_path) / "input.md"
    if not input_md.is_file():
        return []
    keys = keys_in_text(input_md.read_text(encoding="utf-8", errors="replace"))
    return note_docs(keys)


def lookup_fulltext(folder_path, key: str, query: str) -> str:
    """代模型查一篇文献的原文片段,并记入本章 fulltext-lookups.md。"""
    from .references import search_fulltext
    key = (key or "").strip()
    if key not in known_keys():
        return f"Error: unknown reference key '{key}'. Use only keys from the reference index."
    # 即使全文映射残留旧条目,C 级也不能进入检索或留下查阅日志。
    levels_path = Path(_config().INDEX_DIR) / "ref-levels.json"
    try:
        levels = json.loads(levels_path.read_text(encoding="utf-8")) if levels_path.is_file() else {}
    except (OSError, ValueError):
        levels = {}
    if isinstance(levels, dict) and levels.get(key) == "C":
        return (f"Reference '{key}' is C-level (general background): its fulltext is not available. "
                f"Cite it using only its one-line summary.")
    map_path = Path(_config().INDEX_DIR) / "ref-fulltext.json"
    try:
        mapping = json.loads(map_path.read_text(encoding="utf-8")) if map_path.is_file() else {}
    except (OSError, ValueError):
        mapping = {}
    full = mapping.get(key)
    if not full or not Path(full).is_file():
        return f"Reference '{key}' has no fulltext available; rely on its note."
    hits = search_fulltext(Path(full).read_text(encoding="utf-8", errors="replace"), query)
    if not hits:
        return f"No passage of '{key}' matches '{query}'. Try other keywords or rely on the note."
    log = Path(folder_path) / LOOKUP_LOG_NAME
    old = log.read_text(encoding="utf-8") if log.is_file() else "# Fulltext lookups\n\n"
    log.write_text(old + f"- [{key}] query={query!r} paragraphs={[i for i, _ in hits]}\n",
                   encoding="utf-8")
    return "\n\n".join(f"[paragraph {i}]\n{para}" for i, para in hits)


def lookup_for(folder_path):
    """给 run_content_stage 用的 lookup 回调(绑定本章文件夹)。"""
    return lambda key, query: lookup_fulltext(folder_path, key, query)
