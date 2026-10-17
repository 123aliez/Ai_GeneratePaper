"""文献室:inbox/references/ 的解析、匹配、索引与受控读取(纯标准库,不调模型)。

文献室结构(作者维护,系统只读)::

    inbox/references/
    ├── refs.bib                 ← 总 bib(唯一的引用 key 来源)
    ├── <任意文件夹名>/          ← 每篇一个文件夹,名字随意
    │   ├── <笔记>.md           ← 固定模板笔记(YAML 头 + "## 一句话总结" 等小节)
    │   ├── <原文>.md           ← 论文原文(md)
    │   └── assets/            ← 笔记里的图片(忽略)
    └── ...

对号规则:文件夹里带 YAML 头且有 "## 一句话总结" 小节的 md 是笔记,其余 md 是原文。
笔记按头部的 doi → arxiv → 标题 依次去 refs.bib 里找条目;模板占位值视为空。

给模型看的只有两样东西,都由本模块确定性生成到索引目录:
- ref-index.md:每篇一行(key / 标题 / 年份 / 出处 / tags / 一句话总结 / 有无笔记原文);
- refs/<key>.md:过滤版笔记——去掉 YAML 头、"对我的研究的启发"、"延伸阅读"与图片行。
原文只能经 search_fulltext 按关键词取片段(有长度上限),不整篇进上下文。

引用 key 只认 refs.bib(citable_keys),模型给的任何其它 key 都不是合法引用。
"""

from __future__ import annotations

import hashlib
import os
import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

REFS_BIB_NAME = "refs.bib"
NOTE_MARKER_HEADING = "一句话总结"
# 作者自己的想法与推荐书单:不是被引论文的内容,不给模型看(作者决定)。
HIDDEN_NOTE_SECTIONS = ("对我的研究的启发", "延伸阅读")
REF_INDEX_NAME = "ref-index.md"
REF_NOTES_DIRNAME = "refs"

# 原文片段检索的默认上限:每次最多返回这么多字符 / 段落。
FULLTEXT_MAX_CHARS = 3000
FULLTEXT_MAX_HITS = 4

_SKIP_DIR_NAMES = {"assets", "attachments", "images", "img", "figures"}


# ── BibTeX 解析(平衡花括号,足够应付 Zotero / Google Scholar 导出) ─────────
@dataclass
class BibEntry:
    key: str
    entry_type: str
    fields: dict = field(default_factory=dict)   # 字段名小写 → 去外层括号的原值

    @property
    def title(self) -> str:
        return _strip_tex(self.fields.get("title", ""))

    @property
    def year(self) -> str:
        m = re.search(r"\d{4}", self.fields.get("year", "") or self.fields.get("date", ""))
        return m.group(0) if m else ""

    @property
    def venue(self) -> str:
        for name in ("booktitle", "journal", "publisher", "howpublished"):
            if self.fields.get(name):
                return _strip_tex(self.fields[name])
        return ""

    @property
    def doi(self) -> str:
        return normalize_doi(self.fields.get("doi", ""))

    @property
    def arxiv(self) -> str:
        """arXiv 号:只认 eprint(archiveprefix 为 arxiv 或缺省)与明确指向 arxiv 的字段。"""
        eprint = self.fields.get("eprint", "")
        if eprint and self.fields.get("archiveprefix", "arxiv").lower() == "arxiv":
            aid = normalize_arxiv(eprint)
            if aid:
                return aid
        for name in ("journal", "url", "note", "howpublished"):
            value = self.fields.get(name, "")
            if re.search(r"arxiv", value, re.IGNORECASE):
                aid = normalize_arxiv(value)
                if aid:
                    return aid
        return ""


def _read_braced(text: str, i: int) -> tuple[str, int]:
    """text[i] == '{':返回 (括号内文本, 右括号之后的位置)。不平衡时读到结尾。"""
    depth = 0
    start = i + 1
    while i < len(text):
        ch = text[i]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start:i], i + 1
        i += 1
    return text[start:], len(text)


def _read_quoted(text: str, i: int) -> tuple[str, int]:
    """text[i] == '"':读到下一个不在花括号内的引号。"""
    depth = 0
    start = i + 1
    i += 1
    while i < len(text):
        ch = text[i]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
        elif ch == '"' and depth == 0:
            return text[start:i], i + 1
        i += 1
    return text[start:], len(text)


def _read_value(body: str, i: int) -> tuple[str, int]:
    if i >= len(body):
        return "", i
    if body[i] == "{":
        return _read_braced(body, i)
    if body[i] == '"':
        return _read_quoted(body, i)
    vm = re.compile(r"[^,}\s#]+").match(body, i)
    return (vm.group(0), vm.end()) if vm else ("", i + 1)


def _parse_fields(body: str) -> dict:
    fields: dict = {}
    i = 0
    n = len(body)
    while i < n:
        m = re.compile(r"\s*,?\s*([A-Za-z][\w\-:]*)\s*=\s*").match(body, i)
        if not m:
            break
        name = m.group(1).lower()
        i = m.end()
        if i >= n:
            break
        value, i = _read_value(body, i)
        while True:                       # title = {A} # {B}:'#' 拼接
            hm = re.compile(r"\s*#\s*").match(body, i)
            if not hm:
                break
            piece, i = _read_value(body, hm.end())
            value += piece
        fields[name] = re.sub(r"\s+", " ", value).strip()
    return fields


def parse_bib(text: str) -> tuple[dict, list[str]]:
    """BibTeX 文本 → ({key: BibEntry}, 错误清单)。

    重复 key(忽略大小写,BibTeX 的 key 不区分大小写)是错误;@comment / @string /
    @preamble 跳过;解析完一个条目后从它的结尾继续找下一个 @,所以字段值里出现的
    `@word{` 不会被误当成新条目。
    """
    entries: dict = {}
    errors: list[str] = []
    lowered: dict = {}
    pos = 0
    head = re.compile(r"@\s*([A-Za-z]+)\s*([{(])")
    while True:
        m = head.search(text, pos)
        if not m:
            break
        etype = m.group(1).lower()
        open_pos = m.end() - 1
        if text[open_pos] == "{":
            body, end = _read_braced(text, open_pos)
            if text[open_pos:end].count("{") != text[open_pos:end].count("}"):
                errors.append(f"refs.bib 第 {text.count(chr(10), 0, m.start()) + 1} 行的 @{etype} 条目没有闭合的右花括号")
        else:
            close = text.find(")", open_pos)
            end = close + 1 if close != -1 else len(text)
            body = text[open_pos + 1: end - 1 if close != -1 else end]
        pos = max(end, m.end())
        if etype in ("comment", "string", "preamble"):
            continue
        km = re.match(r"\s*([^,\s}]+)\s*(?:,|$)", body)
        if not km:
            line = text.count("\n", 0, m.start()) + 1
            errors.append(f"refs.bib 第 {line} 行的 @{etype} 条目缺少引用 key")
            continue
        key = km.group(1).strip()
        if key.lower() in lowered:
            errors.append(f"refs.bib 里引用 key '{key}' 重复出现"
                          f"(BibTeX 不区分大小写,与 '{lowered[key.lower()]}' 冲突)"
                          if key != lowered[key.lower()] else f"refs.bib 里引用 key '{key}' 重复出现")
            continue
        lowered[key.lower()] = key
        entries[key] = BibEntry(key=key, entry_type=etype,
                                fields=_parse_fields(body[km.end():]))
    if not entries and not errors and re.sub(r"(?m)^\s*%.*$", "", text).strip():
        errors.append("refs.bib 有内容,但一个 @type{key, ...} 条目都没解析出来(格式不对?)")
    return entries, errors


def load_bib(bib_path) -> tuple[dict, list[str]]:
    path = Path(bib_path)
    if not path.is_file():
        return {}, []
    try:
        return parse_bib(path.read_text(encoding="utf-8-sig", errors="replace"))
    except OSError as exc:
        return {}, [f"读不了 {path.name}: {exc}"]


def citable_keys(bib_path) -> set:
    """合法引用 key = refs.bib 里的全部 key。"""
    return set(load_bib(bib_path)[0])


def bib_entry_text(bib_path, keys) -> str:
    """从 refs.bib 原文里逐字抽出指定 key 的条目(组装 LaTeX 工程时用)。花括号与圆括号条目
    都认,和 parse_bib 用同一套定位。"""
    path = Path(bib_path)
    if not path.is_file():
        return ""
    text = path.read_text(encoding="utf-8-sig", errors="replace")
    wanted = set(keys)
    entries, _ = parse_bib(text)
    changed = True
    while changed:                                  # crossref 依赖的条目一并导出
        changed = False
        for k in list(wanted):
            ref = entries[k].fields.get("crossref") if k in entries else None
            if ref and ref in entries and ref not in wanted:
                wanted.add(ref)
                changed = True
    out, pos = [], 0
    head = re.compile(r"@\s*([A-Za-z]+)\s*([{(])")
    while True:
        m = head.search(text, pos)
        if not m:
            break
        open_pos = m.end() - 1
        if text[open_pos] == "{":
            body, end = _read_braced(text, open_pos)
        else:
            close = text.find(")", open_pos)
            end = close + 1 if close != -1 else len(text)
            body = text[open_pos + 1: end - 1 if close != -1 else end]
        pos = max(end, m.end())
        if m.group(1).lower() in ("comment", "string", "preamble"):
            continue
        km = re.match(r"\s*([^,\s}]+)\s*(?:,|$)", body)
        if km and km.group(1).strip() in wanted:
            out.append(text[m.start():end].strip())
    return "\n\n".join(out) + ("\n" if out else "")


# ── 规范化 ───────────────────────────────────────────────────────────────
def _strip_tex(s: str) -> str:
    s = re.sub(r"\\[a-zA-Z]+\s*", "", s or "")
    s = s.replace("{", "").replace("}", "")
    return re.sub(r"\s+", " ", s).strip()


_TEX_ACCENTS = {'"': "\u0308", "'": "\u0301", "`": "\u0300", "^": "\u0302",
                "~": "\u0303", "=": "\u0304", ".": "\u0307"}
_TEX_LETTERS = {"ss": "ß", "o": "ø", "O": "Ø", "aa": "å", "AA": "Å", "ae": "æ", "AE": "Æ",
                "l": "ł", "L": "Ł", "i": "ı"}
_TEX_ACCENT_RE = re.compile(r"\\([\"'`^~=.])\s*\{?\\?([A-Za-z])\}?")
_TEX_LETTER_RE = re.compile(r"\\(ss|aa|AA|ae|AE|o|O|l|L|i)(?![A-Za-z])\s*")


def _tex_to_unicode(s: str) -> str:
    """BibTeX 题名里的 TeX 转义 → Unicode:M{\\"u}ller → Müller,100\\% → 100%。"""
    s = s.replace("\\%", "%").replace("\\&", "&").replace("\\_", "_").replace("\\#", "#")
    s = _TEX_ACCENT_RE.sub(lambda m: m.group(2) + _TEX_ACCENTS[m.group(1)], s)
    return _TEX_LETTER_RE.sub(lambda m: _TEX_LETTERS[m.group(1)], s)


def normalize_title(s: str) -> str:
    s = _strip_tex(_tex_to_unicode(s or ""))
    s = unicodedata.normalize("NFKD", s).lower()
    return "".join(ch for ch in s if ch.isalnum())


def normalize_doi(s: str) -> str:
    s = (s or "").strip().lower()
    s = re.sub(r"^(https?://(dx\.)?doi\.org/|doi:\s*)", "", s)
    return s if re.match(r"^10\.\d{4,9}/\S+$", s) else ""


_ARXIV_RE = re.compile(r"(?<![\d./])(\d{2}(?:0[1-9]|1[0-2])\.\d{4,5})(?:v\d+)?(?!\d)")


def normalize_arxiv(s: str) -> str:
    m = _ARXIV_RE.search(s or "")
    return m.group(1) if m else ""


def _is_placeholder(value) -> bool:
    """模板占位值(未填写)视为空:xxxx、模板里的提示文字。"""
    if value is None:
        return True
    if isinstance(value, list):
        return all(_is_placeholder(v) for v in value)
    v = str(value).strip()
    if not v:
        return True
    lowered = v.lower()
    if "xxxx" in lowered or "10.xxxx" in lowered:
        return True
    return v in ("论文完整标题", "会议/期刊名", "作者1", "作者2", "简称")


# ── 笔记解析 ─────────────────────────────────────────────────────────────
def parse_front_matter(text: str) -> tuple[dict, str]:
    """解析 `---` 包围的 YAML 头(只支持模板用到的子集:标量、[a, b] 列表、- 列表)。"""
    text = text.lstrip("\ufeff")
    if not text.startswith("---"):
        return {}, text
    lines = text.splitlines()
    end = None
    for idx in range(1, len(lines)):
        if lines[idx].strip() == "---":
            end = idx
            break
    if end is None:
        return {}, text
    meta: dict = {}
    current = None
    for raw in lines[1:end]:
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        item = re.match(r"^\s*-\s*(.*)$", raw)
        if item and current is not None:
            meta.setdefault(current, [])
            if isinstance(meta[current], list):
                meta[current].append(_unquote(item.group(1)))
            continue
        kv = re.match(r"^([A-Za-z_][\w\-]*)\s*[:：]\s*(.*)$", raw)
        if not kv:
            continue
        key, value = kv.group(1).lower(), kv.group(2).strip()
        current = key
        if not value:
            meta[key] = []
        elif value.startswith("[") and value.endswith("]"):
            inner = value[1:-1].strip()
            meta[key] = [_unquote(v) for v in _split_list(inner)] if inner else []
        else:
            meta[key] = _unquote(value)
    body = "\n".join(lines[end + 1:])
    return meta, body


def _split_list(inner: str) -> list[str]:
    parts, buf, quote = [], "", None
    for ch in inner:
        if quote:
            buf += ch
            if ch == quote:
                quote = None
        elif ch in "\"'":
            quote = ch
            buf += ch
        elif ch == ",":
            parts.append(buf)
            buf = ""
        else:
            buf += ch
    if buf.strip():
        parts.append(buf)
    return [p.strip() for p in parts if p.strip()]


def _unquote(value: str) -> str:
    v = value.strip()
    if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
        return v[1:-1]
    return v


_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*?)\s*$")


def split_sections(body: str) -> list[tuple[str, str]]:
    """按 `## ` 级标题切块:[(标题, 块文本含标题行)]。标题前的内容标题记为 ""。"""
    blocks: list[tuple[str, list[str]]] = [("", [])]
    for line in body.splitlines():
        m = _HEADING_RE.match(line)
        if m and len(m.group(1)) == 2:
            blocks.append((m.group(2), [line]))
        else:
            blocks[-1][1].append(line)
    return [(title, "\n".join(lines)) for title, lines in blocks]


def is_note(text: str) -> bool:
    meta, body = parse_front_matter(text)
    if not meta:
        return False
    return any(NOTE_MARKER_HEADING in title for title, _ in split_sections(body))


def note_summary(text: str) -> str:
    """"## 一句话总结" 小节的正文,压成一行。"""
    _meta, body = parse_front_matter(text)
    for title, block in split_sections(body):
        if NOTE_MARKER_HEADING in title:
            content = "\n".join(block.splitlines()[1:])
            content = re.sub(r"!\[\[.*?\]\]|!\[[^\]]*\]\([^)]*\)", "", content)
            return re.sub(r"\s+", " ", content).strip()
    return ""


_IMAGE_LINE_RE = re.compile(r"^\s*(?:!\[\[.*?\]\]|!\[[^\]]*\]\([^)]*\))\s*$")


def filter_note(text: str) -> str:
    """过滤版笔记:去 YAML 头、去作者私人小节、去图片行(图下讲解文字保留)。"""
    _meta, body = parse_front_matter(text)
    kept: list[str] = []
    skip_level = 0                       # >0:正在跳过一个私人小节(直到出现同级或更浅的标题)
    for ln in body.splitlines():
        m = _HEADING_RE.match(ln)
        if m:
            level = len(m.group(1))
            if skip_level and level <= skip_level:
                skip_level = 0
            if not skip_level and any(h in m.group(2) for h in HIDDEN_NOTE_SECTIONS):
                skip_level = level       # 任何层级(## / ### / ####)的私人标题都过滤
        if skip_level or _IMAGE_LINE_RE.match(ln):
            continue
        kept.append(ln)
    return re.sub(r"\n{3,}", "\n\n", "\n".join(kept)).strip() + "\n"


# ── 文献室扫描与对号 ────────────────────────────────────────────────────
@dataclass
class PaperRecord:
    key: str
    folder: str                      # 相对文献室的文件夹名
    note_path: Path | None
    fulltext_path: Path | None
    meta: dict
    summary: str
    tags: list
    match: str                       # doi / arxiv / title


@dataclass
class Library:
    root: Path
    bib_path: Path
    entries: dict = field(default_factory=dict)      # key → BibEntry
    records: dict = field(default_factory=dict)      # key → PaperRecord
    errors: list = field(default_factory=list)       # 必须先修(--go 硬停)
    warnings: list = field(default_factory=list)     # 提醒,不阻塞

    def keys(self) -> set:
        return set(self.entries)


def _match_entry(meta: dict, entries: dict) -> tuple[str | None, str]:
    """笔记头部 → bib key。返回 (key, 依据) 或 (None, 失败原因)。"""
    doi = "" if _is_placeholder(meta.get("doi")) else normalize_doi(str(meta.get("doi")))
    if doi:
        hits = [k for k, e in entries.items() if e.doi == doi]
        if len(hits) == 1:
            return hits[0], "doi"
        if len(hits) > 1:
            return None, f"doi {doi} 对应多条 bib 条目: {', '.join(sorted(hits))}"
    arxiv = "" if _is_placeholder(meta.get("arxiv")) else normalize_arxiv(str(meta.get("arxiv")))
    if arxiv:
        hits = [k for k, e in entries.items() if e.arxiv == arxiv]
        if len(hits) == 1:
            return hits[0], "arxiv"
        if len(hits) > 1:
            return None, f"arxiv {arxiv} 对应多条 bib 条目: {', '.join(sorted(hits))}"
    title = "" if _is_placeholder(meta.get("title")) else normalize_title(str(meta.get("title")))
    if title:
        hits = [k for k, e in entries.items() if normalize_title(e.title) == title]
        if len(hits) == 1:
            return hits[0], "title"
        if len(hits) > 1:
            return None, f"标题对应多条 bib 条目: {', '.join(sorted(hits))}"
    if not (doi or arxiv or title):
        return None, "笔记头部的 doi / arxiv / title 都没填"
    return None, "按 doi / arxiv / 标题都在 refs.bib 里找不到对应条目"


def _md_files(folder: Path) -> list[Path]:
    files = []
    for path in sorted(folder.rglob("*.md")):
        if path.is_symlink() or any(p.is_symlink() for p in path.relative_to(folder).parents
                                    if False) or path.resolve().parent != path.parent.resolve():
            continue                     # 不跟随符号链接:链接可能指向 inbox 之外的文件
        rel_parts = path.relative_to(folder).parts
        if any(p.startswith(".") or p.lower() in _SKIP_DIR_NAMES for p in rel_parts[:-1]):
            continue
        files.append(path)
    return files


def scan_library(ref_root, bib_path=None) -> Library:
    """扫描文献室,完成 bib ↔ 笔记 ↔ 原文 对号。只读。"""
    root = Path(ref_root)
    bib = Path(bib_path) if bib_path else root / REFS_BIB_NAME
    lib = Library(root=root, bib_path=bib)
    if not root.is_dir():
        lib.warnings.append("没有 references/ 文件夹:正文不能引用任何文献")
        return lib
    folders = [p for p in sorted(root.iterdir())
               if p.is_dir() and not p.is_symlink() and not p.name.startswith((".", "_"))]
    for p in sorted(root.iterdir()):
        if p.is_symlink():
            lib.errors.append(f"{p.name} 是符号链接,文献室不跟随符号链接(请放真实文件)")
    if not bib.is_file():
        if folders:
            lib.errors.append(f"有 {len(folders)} 个文献文件夹,但缺少 {REFS_BIB_NAME}"
                              f"(引用 key 只认 refs.bib)")
        else:
            lib.warnings.append(f"references/ 里没有 {REFS_BIB_NAME}:正文不能引用任何文献")
        return lib
    lib.entries, bib_errors = load_bib(bib)
    lib.errors.extend(bib_errors)

    owner: dict = {}
    for folder in folders:
        mds = _md_files(folder)
        notes, others = [], []
        for path in mds:
            try:
                text = path.read_text(encoding="utf-8-sig", errors="replace")
            except OSError as exc:
                lib.errors.append(f"{folder.name}/{path.name} 读不了: {exc}")
                continue
            (notes if is_note(text) else others).append((path, text))
        if not notes:
            msg = (f"{folder.name}/ 里没有找到模板笔记"
                   f"(带 YAML 头和 '## {NOTE_MARKER_HEADING}' 的 md),这篇文献不会进入索引")
            (lib.errors if mds else lib.warnings).append(msg)
            continue
        if len(notes) > 1:
            lib.errors.append(f"{folder.name}/ 里有 {len(notes)} 份笔记"
                              f"({', '.join(p.name for p, _ in notes)}),每篇只能一份")
            continue
        note_path, note_text = notes[0]
        meta, _body = parse_front_matter(note_text)
        key, how = _match_entry(meta, lib.entries)
        if key is None:
            lib.errors.append(f"{folder.name}/{note_path.name}: {how}")
            continue
        if key in owner:
            lib.errors.append(f"{folder.name}/ 与 {owner[key]}/ 都对应 bib 条目 '{key}'")
            continue
        owner[key] = folder.name
        fulltext = None
        if others:
            others.sort(key=lambda pt: len(pt[1]), reverse=True)
            fulltext = others[0][0]
            if len(others) > 1:
                lib.warnings.append(f"{folder.name}/ 有 {len(others)} 个非笔记 md,"
                                    f"取最长的 {fulltext.name} 作原文")
        else:
            lib.warnings.append(f"{folder.name}/ 没有原文 md(只能读笔记)")
        entry = lib.entries[key]
        note_year = "" if _is_placeholder(meta.get("year")) else str(meta.get("year")).strip()
        if note_year and entry.year and note_year != entry.year:
            lib.warnings.append(f"{folder.name}/ 笔记年份 {note_year} 与 bib '{key}' 的 {entry.year} 不一致")
        tags = meta.get("tags") if isinstance(meta.get("tags"), list) else []
        lib.records[key] = PaperRecord(
            key=key, folder=folder.name, note_path=note_path, fulltext_path=fulltext,
            meta=meta, summary=note_summary(note_text),
            tags=[t for t in tags if not _is_placeholder(t)], match=how)
    for key in sorted(set(lib.entries) - set(lib.records)):
        lib.warnings.append(f"bib 条目 '{key}' 没有笔记文件夹:只能按题录引用,不能描述其内容")
    return lib


# ── 索引生成 ─────────────────────────────────────────────────────────────
def _cell(text: str, limit: int = 0) -> str:
    t = re.sub(r"\s+", " ", str(text or "")).replace("|", "/").strip()
    if limit and len(t) > limit:
        t = t[:limit - 1] + "…"
    return t


def render_ref_index(lib: Library) -> str:
    lines = [
        "# 文献索引(程序生成,勿手改;源头是 inbox/references/)",
        "",
        "引用只能用下表第一列的 key:`\\cite{key}`。先看一句话总结挑文献,再用",
        "read_reference(key) 读过滤版笔记;需要原文细节时用 search_reference_text(key, 关键词)。",
        "标“无笔记”的条目只知道题录,不要描述它的内容。",
        "",
        "| key | 标题 | 年份 | 出处 | tags | 一句话总结 | 笔记 | 原文 |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for key in sorted(lib.entries):
        entry = lib.entries[key]
        rec = lib.records.get(key)
        summary = rec.summary if rec else "(无笔记)"
        tags = ", ".join(rec.tags) if rec else ""
        lines.append(
            f"| {key} | {_cell(entry.title, 160)} | {entry.year} | {_cell(entry.venue, 60)} | "
            f"{_cell(tags, 60)} | {_cell(summary, 240)} | {'有' if rec else '无'} | "
            f"{'有' if rec and rec.fulltext_path else '无'} |")
    return "\n".join(lines) + "\n"


def _write_if_changed(path: Path, text: str) -> bool:
    if path.is_file() and path.read_text(encoding="utf-8", errors="replace") == text:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)
    return True


def build_reference_index(lib: Library, index_dir) -> Path:
    """写 ref-index.md 与 refs/<key>.md(过滤版笔记)。内容不变不重写。"""
    index_dir = Path(index_dir)
    notes_dir = index_dir / REF_NOTES_DIRNAME
    notes_dir.mkdir(parents=True, exist_ok=True)
    wanted = set()
    for key, rec in lib.records.items():
        entry = lib.entries[key]
        head = (f"# [{key}] {entry.title}\n\n"
                f"- 出处: {entry.venue or '?'} {entry.year}\n"
                f"- 原文: {'有,可用 search_reference_text 取片段' if rec.fulltext_path else '无'}\n\n")
        text = rec.note_path.read_text(encoding="utf-8", errors="replace")
        name = f"{_safe_name(key)}.md"
        wanted.add(name)
        _write_if_changed(notes_dir / name, head + filter_note(text))
    for stale in notes_dir.glob("*.md"):
        if stale.name not in wanted:
            stale.unlink()
    import json
    fulltext = {k: str(r.fulltext_path) for k, r in sorted(lib.records.items()) if r.fulltext_path}
    _write_if_changed(index_dir / "ref-fulltext.json",
                      json.dumps(fulltext, ensure_ascii=False, indent=1) + "\n")
    out = index_dir / REF_INDEX_NAME
    _write_if_changed(out, render_ref_index(lib))
    return out


def _safe_name(key: str) -> str:
    safe = re.sub(r"[^\w.\-]+", "_", key)
    if safe != key:               # Smith:2020 与 Smith_2020 不能落到同一个文件
        safe += "-" + hashlib.sha1(key.encode("utf-8")).hexdigest()[:6]
    return safe


def note_view_path(index_dir, key: str) -> Path:
    return Path(index_dir) / REF_NOTES_DIRNAME / f"{_safe_name(key)}.md"


def notes_fingerprint(index_dir, keys) -> str:
    """一组文献过滤版笔记的内容指纹(章节输入指纹用:用到的笔记变了才判陈旧)。"""
    digest = hashlib.sha256()
    for key in sorted(set(keys)):
        path = note_view_path(index_dir, key)
        digest.update(key.encode("utf-8"))
        digest.update(b"\0")
        if path.is_file():
            digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()[:16]


# ── 原文片段检索 ─────────────────────────────────────────────────────────
def _terms(query: str) -> list[str]:
    tokens = [t for t in re.split(r"[\s,;，；、。.:：()（）\[\]\"'“”]+", (query or "").lower()) if t]
    terms = []
    for tok in tokens:
        if re.search(r"[一-鿿]", tok) and len(tok) > 2:
            # 中文没有空格:整词 + 二字片段,保证"注意力机制"能命中"注意力"。
            terms.append(tok)
            terms.extend(tok[i:i + 2] for i in range(len(tok) - 1))
        elif len(tok) >= 2:
            terms.append(tok)
    return list(dict.fromkeys(terms))


def search_fulltext(text: str, query: str, max_chars: int = FULLTEXT_MAX_CHARS,
                    max_hits: int = FULLTEXT_MAX_HITS) -> list[tuple[int, str]]:
    """按关键词给段落打分,返回 [(段号, 段落)](按原文顺序),总长不超过 max_chars。"""
    terms = _terms(query)
    if not terms:
        return []
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", text or "") if p.strip()]
    scored = []
    for idx, para in enumerate(paragraphs, start=1):
        low = para.lower()
        score = sum(low.count(t) * (2 if len(t) > 2 else 1) for t in terms)
        if score:
            scored.append((score, idx, para))
    scored.sort(key=lambda s: (-s[0], s[1]))
    picked, used = [], 0
    for _score, idx, para in scored[:max_hits]:
        room = max_chars - used
        if room <= 80:
            break
        if len(para) > room:
            para = para[:room - 1] + "…"
        picked.append((idx, para))
        used += len(para)
    return sorted(picked)
