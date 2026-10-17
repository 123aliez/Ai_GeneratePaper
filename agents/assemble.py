"""整篇组装(纯标准库,不调模型,不编译):各章终稿 → 交付物。

输出到 output/:
- paper.md / paper.zh.md   整篇初稿(英文 + 中文审阅版);`[TABLE: ...]` 已按 CSV 渲染,
                            `[[TAB:x]]` / `[[FIG:x]]` 已换成 Table N / Figure N
- latex/                   LaTeX 工程:main.tex + sections/*.tex + refs.bib(只含被引条目)
- 待补清单.md               缺数据 / 缺引用 / 缺设计细节 / 需核对的代码细节 / 未核验原因,按章列出
- 图表清单.md               每张图的编号、图题、数据来源和**绘图提示词**
- 数字溯源表.md             正文每个数字 → 值 → CSV 文件:行

LaTeX 不在本系统编译;md→tex 是一个保守的转换器(标题/段落/列表/表格/公式/引用),
转换后会做静态检查(括号配平、引用 key 都在 refs.bib 里、标签不重复)。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from . import results_tables as rt
from .citation_supplement import unknown_cite_keys
from .references import bib_entry_text, load_bib

MARKERS = [
    ("MISSING DATA", "缺数据:结果表里没有这个数字"),
    ("MISSING RESULT", "缺结果:结果表里没有这个结果"),
    ("UNKNOWN", "未知:材料里没有这个信息"),
    ("CITATION NEEDED", "缺引用:需要补一条文献"),
    ("DESIGN DETAIL NEEDED", "缺设计细节:idea.md 没写到"),
    ("IDEA NEEDED", "缺 idea:idea.md 没写到"),
    ("REFERENCE NEEDED", "缺文献:文献库里没有合适的"),
    ("FROM CODE — 需核对", "来自代码静态解析,需核对是否与实际实验一致"),
]
_MARKER_RE = re.compile(r"\[(" + "|".join(re.escape(m) for m, _ in MARKERS) + r")(?::[^\]]*)?\]")
_UNKNOWN_RES_RE = re.compile(r"⟨未知结果 ([^⟩]+)⟩")
_FIG_HEAD_RE = re.compile(r"^\[FIGURE\s+([\w\-]+)\s*:\s*(.+?)\]\s*$")
_REF_RE = re.compile(r"\[\[(TAB|FIG):([^\]]+)\]\]")
_LEADING_NUM_RE = re.compile(r"^\s*\d+(?:\.\d+)*[.)、]?\s+")


# ── 数据结构 ─────────────────────────────────────────────────────────────
@dataclass
class Chapter:
    folder: str
    title: str
    ctype: str
    number: int
    en: str = ""
    zh: str = ""
    status: str = ""                  # verified / unverified / ""
    reasons: list = field(default_factory=list)
    todo: str = ""
    used: list = field(default_factory=list)      # numbers-used.json 的 used 列表


@dataclass
class Figure:
    number: int
    fid: str
    title: str
    chapter: str
    fields: dict


# ── 读取章节 ─────────────────────────────────────────────────────────────
def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace") if path.is_file() else ""


def load_chapters(chapters_meta: list[dict], workspace: Path) -> list[Chapter]:
    out = []
    for meta in chapters_meta:
        folder = workspace / meta["folder"]
        ch = Chapter(folder=meta["folder"], title=meta["title"], ctype=meta.get("type", ""),
                     number=int(meta.get("number", len(out) + 1)),
                     en=_read(folder / "final.md"), zh=_read(folder / "final.zh.md"),
                     todo=_read(folder / "todo.md"))
        vtext = _read(folder / "verification.md")
        if "Status: UNVERIFIED" in vtext:
            ch.status = "unverified"
            ch.reasons = [ln[2:].strip() for ln in vtext.splitlines() if ln.startswith("- ")]
        elif "Status: VERIFIED" in vtext:
            ch.status = "verified"
        try:
            ch.used = json.loads(_read(folder / "numbers-used.json") or "{}").get("used", [])
        except ValueError:
            ch.used = []
        out.append(ch)
    return out


# ── 图/表占位符 ───────────────────────────────────────────────────────────
def parse_figure_blocks(text: str) -> list[dict]:
    """文本里的 [FIGURE id: 图题] 占位块 → [{id,title,start,end,fields}]。块到空行结束。"""
    lines = text.splitlines()
    blocks, i = [], 0
    while i < len(lines):
        m = _FIG_HEAD_RE.match(lines[i].strip())
        if not m:
            i += 1
            continue
        j = i + 1
        body = []
        while (j < len(lines) and lines[j].strip() and not lines[j].startswith("\x00H")
               and not lines[j].strip().startswith(("[FIGURE", "[TABLE"))):
            body.append(lines[j].strip().replace("\x00", ""))
            j += 1
        fields: dict = {}
        key = None
        for ln in body:
            fm = re.match(r"^(Type|Note|Data|Source|Supports)\s*[.:：]\s*(.*)$", ln, re.IGNORECASE)
            if fm:
                key = fm.group(1).capitalize()
                fields[key] = fm.group(2).strip()
            elif key:
                fields[key] += " " + ln
        blocks.append({"id": m.group(1), "title": m.group(2).strip(), "start": i, "end": j,
                       "fields": fields})
        i = j
    return blocks


def _slug(text: str) -> str:
    """LaTeX 标签 / 文件名用的 ASCII 短名。含非 ASCII(如中文表名)时加哈希后缀,
    保证不同名字不会撞成同一个标签。"""
    base = re.sub(r"[^A-Za-z0-9]+", "-", text).strip("-").lower()
    if text.isascii() and base:
        return base
    import hashlib
    return (base or "x") + "-" + hashlib.sha1(text.encode("utf-8")).hexdigest()[:6]


def _strip_num(title: str) -> str:
    return _LEADING_NUM_RE.sub("", title).strip()


def normalize_headings(text: str, chapter_title: str) -> tuple[str, list[tuple[int, str]]]:
    """返回 (正文, [(相对层级, 标题)])。相对层级 1 = 章下一级小节。

    规则:最浅层级的标题恰好一个、是第一个标题、且后面还有更深的小节标题 → 它就是章标题
    (丢弃,用大纲里的章名);否则它本身就是小节(不删)。代码围栏与 $$ 公式块里的 `#` 行
    不是标题。标题行在正文里以哨兵 `\x00H<level>\x00<title>` 表示;输入里本来就带的
    控制字符先清掉,免得伪造哨兵。
    """
    text = text.replace("\x00", "")
    lines = text.splitlines()
    heads, fence, math = [], False, False
    for i, ln in enumerate(lines):
        st = ln.strip()
        if st.startswith("```") or st.startswith("~~~"):
            fence = not fence
            continue
        if not fence and st == "$$":
            math = not math
            continue
        if fence or math:
            continue
        m = re.match(r"^(#{1,6})\s+(.*?)\s*$", ln)
        if m:
            heads.append((i, len(m.group(1)), m.group(2)))
    if not heads:
        return text, []
    shallow = min(h[1] for h in heads)
    top = [h for h in heads if h[1] == shallow]
    drop_first = len(top) == 1 and heads[0][1] == shallow and len(heads) > 1
    base = shallow + (1 if drop_first else 0)
    heading_idx = {h[0]: h for h in heads}
    out, collected = [], []
    for i, ln in enumerate(lines):
        if i in heading_idx:
            _, level, title = heading_idx[i]
            if drop_first and level == shallow:
                continue
            rel = max(1, level - base + 1)
            title = _strip_num(title)
            collected.append((rel, title))
            out.append(f"\x00H{rel}\x00{title}")
        else:
            out.append(ln)
    return "\n".join(out), collected


# ── paper.md ─────────────────────────────────────────────────────────────
@dataclass
class Numbering:
    tables: dict = field(default_factory=dict)    # 表名 → 号
    figures: dict = field(default_factory=dict)   # 图 id → 号
    figure_list: list = field(default_factory=list)


def number_items(chapters: list[Chapter]) -> Numbering:
    """按出现顺序给表/图编号(跨章连续)。"""
    num = Numbering()
    for ch in chapters:
        for p in rt.find_table_placeholders(ch.en):
            if p["name"] not in num.tables:
                num.tables[p["name"]] = len(num.tables) + 1
        for b in parse_figure_blocks(ch.en):
            if b["id"] not in num.figures:
                num.figures[b["id"]] = len(num.figures) + 1
                num.figure_list.append(Figure(num.figures[b["id"]], b["id"], b["title"],
                                              ch.folder, b["fields"]))
    return num


def _replace_refs_md(text: str, num: Numbering) -> str:
    def sub(m):
        kind, name = m.group(1), m.group(2).strip()
        table = num.tables if kind == "TAB" else num.figures
        if name in table:
            return f"{'Table' if kind == 'TAB' else 'Figure'} {table[name]}"
        return m.group(0)
    return _REF_RE.sub(sub, text)


def _expand_tables_md(text: str, tables: dict, num: Numbering) -> str:
    by_name = {t.name: t for t in tables.values()}
    def sub(m):
        p = rt.parse_table_placeholder(m.group("body"))
        t = by_name.get(p["name"])
        if t is None or not t.is_grid:
            return f"**[TABLE `{p['name']}` 在结果目录里不存在 — 见待补清单]**"
        return rt.render_table_markdown(t, p["caption"], p["note"], num.tables.get(p["name"], 0))
    return rt.TABLE_PLACEHOLDER_RE.sub(sub, text)


def _figure_block_md(block: dict, number: int) -> str:
    f = block["fields"]
    lines = [f"> **Figure {number}. {block['title']}** *(待补图,见《图表清单.md》)*"]
    if f.get("Note"):
        lines.append(f"> {f['Note']}")
    return "\n".join(lines)


def build_paper_md(chapters: list[Chapter], num: Numbering, tables: dict, lang: str = "en") -> str:
    parts = []
    for ch in chapters:
        text = ch.en if lang == "en" else (ch.zh or ch.en)
        if not text.strip():
            parts.append(f"## {ch.title}\n\n**[本章没有生成终稿 — 见待补清单]**\n")
            continue
        body, _heads = normalize_headings(text, ch.title)
        # 图占位块 → 图注块
        lines = body.splitlines()
        blocks = {b["start"]: b for b in parse_figure_blocks(body)}
        out, i = [], 0
        while i < len(lines):
            if i in blocks:
                b = blocks[i]
                out.append(_figure_block_md(b, num.figures.get(b["id"], 0)))
                i = b["end"]
                continue
            out.append(lines[i])
            i += 1
        body = "\n".join(out)
        body = _expand_tables_md(body, tables, num)
        body = _replace_refs_md(body, num)
        body = re.sub(r"\x00H(\d)\x00(.*)", lambda m: "#" * (int(m.group(1)) + 2) + " " + m.group(2), body)
        parts.append(f"## {ch.title}\n\n{body.strip()}\n")
    return "\n".join(parts).strip() + "\n"


# ── md → LaTeX ───────────────────────────────────────────────────────────
# 保护区:行内数学 + 白名单命令(引用 / 交叉引用 / 标签 / 少量排版)。白名单之外的反斜杠
# 一律转义——模型写的文本、图题、作者字段不能夹带 \input / \write18 之类的命令。
_SAFE_CMDS = (r"(?:(?:paren|text|auto|foot|smart|super)?cite[a-zA-Z]*|ref|eqref|label|textbf|textit|"
              r"emph|texttt|todo|url|mathbf|mathrm|mathcal|mathbb|boldsymbol)")
_PROTECT_RE = re.compile(
    r"(\$(?!\s)[^$\n]*?(?<!\s)\$"                                           # 行内数学($5 and $10 不算)
    r"|\\\(.+?\\\)"                                                      # \( ... \)
    rf"|\\{_SAFE_CMDS}\*?(?:\[[^\]]*\])*(?:\{{(?:[^{{}}]|\{{[^{{}}]*\}})*\}})*)")
_DANGEROUS_RE = re.compile(
    r"\\(?:input|include|includeonly|write|openout|openin|read|immediate|catcode|def|edef|gdef|xdef|"
    r"let|csname|usepackage|RequirePackage|documentclass|newcommand|renewcommand|providecommand|loop|"
    r"special|pdfshellescape|directlua|lstinputlisting|verbatiminput|InputIfFileExists)(?![A-Za-z])"
    r"|\^\^")                                                           # ^^ 字符转换可绕过命令黑名单
_SAFE_ENVS = {"equation", "equation*", "align", "align*", "gather", "gather*", "multline", "multline*",
              "split", "cases", "aligned", "array", "matrix", "pmatrix", "bmatrix"}
_TEX_SPECIALS = {"&": r"\&", "%": r"\%", "_": r"\_", "#": r"\#", "{": r"\{", "}": r"\}",
                 "~": r"\textasciitilde{}", "^": r"\textasciicircum{}", "$": r"\$"}
_UNICODE_TEX = {"≥": r"$\geq$", "≤": r"$\leq$", "×": r"$\times$", "±": r"$\pm$", "→": r"$\rightarrow$",
                "←": r"$\leftarrow$", "↑": r"$\uparrow$", "↓": r"$\downarrow$", "≈": r"$\approx$",
                "≠": r"$\neq$", "∈": r"$\in$", "∞": r"$\infty$", "−": "-", "–": "--", "—": "---",
                "“": "``", "”": "''", "‘": "`", "’": "'", "…": r"\ldots{}", "†": r"\dag{}"}


def _escape_plain(text: str) -> str:
    out = []
    for ch in text:
        out.append(r"\textbackslash{}" if ch == "\\" else _TEX_SPECIALS.get(ch, _UNICODE_TEX.get(ch, ch)))
    return "".join(out)


def _inline_to_tex(text: str, num: Numbering) -> str:
    # 交叉引用
    def ref(m):
        kind, name = m.group(1), m.group(2).strip()
        table = num.tables if kind == "TAB" else num.figures
        if name in table:
            return ("Table " if kind == "TAB" else "Figure ") + r"\ref{" + \
                   ("tab:" if kind == "TAB" else "fig:") + _slug(name) + "}"
        return f"[{kind}:{name}?]"
    text = _REF_RE.sub(ref, text)
    # 标记 → \todo
    text = _MARKER_RE.sub(lambda m: "\x01" + m.group(1) + "\x02", text)
    text = _UNKNOWN_RES_RE.sub(lambda m: "\x01unknown result " + m.group(1) + "\x02", text)
    pieces, last = [], 0
    for m in _PROTECT_RE.finditer(text):
        pieces.append(_style(_escape_plain(text[last:m.start()])))
        span = m.group(0)
        if _DANGEROUS_RE.search(span):
            span = _escape_plain(span)
        elif re.match(r"\\(?:textbf|textit|emph|texttt)\{", span):
            span = re.sub(r"(?<!\\)([&%#_])", r"\\\1", span)      # 排版命令参数里的特殊字符仍要转义
        pieces.append(span)
        last = m.end()
    pieces.append(_style(_escape_plain(text[last:])))
    out = "".join(pieces)
    return re.sub(r"\x01([^\x02]*)\x02",
                  lambda m: r"\todo{" + m.group(1).replace("—", "---").replace("需核对", "to check") + "}", out)


def _style(escaped: str) -> str:
    escaped = re.sub(r"`([^`]+)`", r"\\texttt{\1}", escaped)
    escaped = re.sub(r"\*\*([^*]+)\*\*", r"\\textbf{\1}", escaped)
    escaped = re.sub(r"(?<![\w*])\*([^*\n]+)\*(?![\w*])", r"\\textit{\1}", escaped)
    escaped = re.sub(r"\[([^\]]+)\]\((https?://[^)]+)\)", r"\1", escaped)
    return escaped


def _md_table_to_tex(rows: list[list[str]], num: Numbering) -> str:
    header, body = rows[0], rows[1:]
    out = [r"\begin{table}[t]", r"\centering", rf"\begin{{tabular}}{{{'l' + 'r' * (len(header) - 1)}}}",
           r"\toprule", " & ".join(_inline_to_tex(c, num) for c in header) + r" \\", r"\midrule"]
    for r in body:
        out.append(" & ".join(_inline_to_tex(c, num) for c in r) + r" \\")
    out += [r"\bottomrule", r"\end{tabular}", r"\end{table}"]
    return "\n".join(out)


def _figure_tex(block: dict, number: int) -> str:
    f = block["fields"]
    caption = block["title"] + (". " + f["Note"] if f.get("Note") else "")
    stem = f"figures/fig{number}-{_slug(block['id'])}"
    box = (rf"\IfFileExists{{{stem}.pdf}}{{\includegraphics[width=0.8\linewidth]{{{stem}.pdf}}}}"
           rf"{{\IfFileExists{{{stem}.png}}{{\includegraphics[width=0.8\linewidth]{{{stem}.png}}}}"
           r"{\fbox{\parbox[c][3cm][c]{0.8\linewidth}{\centering Figure " + str(number) +
           r" --- to be supplied\\ (see the figure list)}}}}")
    return "\n".join([r"\begin{figure}[t]", r"\centering", box,
                      rf"\caption{{{_escape_plain(caption)}}}", rf"\label{{fig:{_slug(block['id'])}}}",
                      r"\end{figure}"])


def _guard_block(chunk: list[str], env: str | None = None) -> list[str]:
    """原样放行的公式 / 环境块:环境不在白名单、或含 \\input \\write 之类的危险原语时,
    改成转义后的 verbatim 块并标 \\todo,不让模型文本在编译时读文件 / 执行命令。"""
    text = "\n".join(chunk)
    if (env is not None and env not in _SAFE_ENVS) or _DANGEROUS_RE.search(text):
        safe = [ln.replace(r"\end{verbatim}", r"\end {verbatim}") for ln in chunk]
        return [r"\todo{unsafe LaTeX block shown verbatim}", r"\begin{verbatim}", *safe, r"\end{verbatim}"]
    return list(chunk)


def md_to_tex(md: str, num: Numbering, tables: dict, *, level_offset: int = 0) -> str:
    by_name = {t.name: t for t in tables.values()}
    lines = md.splitlines()
    blocks = {b["start"]: b for b in parse_figure_blocks(md)}
    out: list[str] = []
    para: list[str] = []
    list_stack: list[tuple[int, str]] = []        # (缩进, itemize/enumerate)
    i = 0

    def flush_para():
        if para:
            out.append(_inline_to_tex(" ".join(para), num))
            out.append("")
            para.clear()

    def close_lists(to_indent=-1):
        while list_stack and list_stack[-1][0] > to_indent:
            out.append(rf"\end{{{list_stack.pop()[1]}}}")

    while i < len(lines):
        raw = lines[i]
        line = raw.rstrip()
        stripped = line.strip()
        if i in blocks:
            flush_para(); close_lists()
            b = blocks[i]
            out.append(_figure_tex(b, num.figures.get(b["id"], 0)))
            out.append("")
            i = b["end"]
            continue
        hm = re.match(r"^\x00H(\d)\x00(.*)$", stripped)
        if hm:
            flush_para(); close_lists()
            rel = int(hm.group(1)) + level_offset
            cmd = {1: "subsection", 2: "subsubsection"}.get(rel, "paragraph")
            out.append(rf"\{cmd}{{{_inline_to_tex(hm.group(2), num)}}}")
            out.append("")
            i += 1
            continue
        tm = rt.TABLE_PLACEHOLDER_RE.match(line)
        if tm:
            flush_para(); close_lists()
            p = rt.parse_table_placeholder(tm.group("body"))
            t = by_name.get(p["name"])
            if t is not None and t.is_grid:
                out.append(rt.render_table_latex(t, p["caption"], p["note"], f"tab:{_slug(p['name'])}"))
            else:
                out.append(rf"\todo{{table {_escape_plain(p['name'])} not found}}")
            out.append("")
            i += 1
            continue
        if stripped.startswith("```"):
            flush_para(); close_lists()
            j = i + 1
            code = []
            while j < len(lines) and not lines[j].strip().startswith("```"):
                code.append(lines[j])
                j += 1
            code = [ln.replace(r"\end{verbatim}", r"\end {verbatim}") for ln in code]   # 不让内容提前结束环境
            out += [r"\begin{verbatim}", *code, r"\end{verbatim}", ""]
            i = j + 1
            continue
        if stripped.startswith("$$") or stripped == r"\[" or stripped.startswith(r"\begin{"):
            flush_para(); close_lists()
            if stripped.startswith(r"\begin{"):
                env = re.match(r"\\begin\{([^}]+)\}", stripped).group(1)
                j = i
                chunk = []
                while j < len(lines):
                    chunk.append(lines[j])
                    if rf"\end{{{env}}}" in lines[j]:
                        break
                    j += 1
                out += _guard_block(chunk, env) + [""]
                i = j + 1
                continue
            if stripped.startswith(r"\[") and stripped.endswith(r"\]") and len(stripped) > 4:
                out += _guard_block([stripped]) + [""]
                i += 1
                continue
            m_dd = re.match(r"^\$\$(.+?)\$\$(.*)$", stripped)
            if m_dd and m_dd.group(2).strip():
                out += _guard_block([r"\[", m_dd.group(1).strip(), r"\]"]) + [""]
                para.append(m_dd.group(2).strip())
                i += 1
                continue
            if stripped.startswith("$$") and stripped.endswith("$$") and len(stripped) > 4:
                out += _guard_block([r"\[", stripped[2:-2].strip(), r"\]"]) + [""]
                i += 1
                continue
            closer = "$$" if stripped.startswith("$$") else r"\]"
            j = i + 1
            chunk = [stripped[2:].strip()] if stripped.startswith("$$") and len(stripped) > 2 else []
            while j < len(lines) and lines[j].strip() != closer and not lines[j].strip().endswith(closer):
                chunk.append(lines[j])
                j += 1
            if j < len(lines) and lines[j].strip() not in (closer,):
                chunk.append(lines[j].strip()[: -len(closer)])
            out += _guard_block([r"\[", *chunk, r"\]"]) + [""]
            i = j + 1
            continue
        if "|" in stripped and stripped.startswith("|") and i + 1 < len(lines) and \
                re.match(r"^\s*\|?[\s:\-|]+\|[\s:\-|]*$", lines[i + 1]):
            flush_para(); close_lists()
            rows = []
            j = i
            while j < len(lines) and lines[j].strip().startswith("|"):
                if not re.match(r"^\s*\|?[\s:\-|]+\|[\s:\-|]*$", lines[j]):
                    rows.append([c.strip() for c in lines[j].strip().strip("|").split("|")])
                j += 1
            out += [_md_table_to_tex(rows, num), ""]
            i = j
            continue
        bm = re.match(r"^(\s*)([-*+]|\d+[.)])\s+(.*)$", line)
        if bm:
            flush_para()
            indent = len(bm.group(1).expandtabs(4))
            kind = "enumerate" if bm.group(2)[0].isdigit() else "itemize"
            close_lists(indent)
            if list_stack and list_stack[-1][0] == indent and list_stack[-1][1] != kind:
                out.append(rf"\end{{{list_stack.pop()[1]}}}")
            if not list_stack or list_stack[-1][0] < indent:
                out.append(rf"\begin{{{kind}}}")
                list_stack.append((indent, kind))
            out.append(r"\item " + _inline_to_tex(bm.group(3), num))
            i += 1
            continue
        if not stripped:
            flush_para()
            nxt = next((l for l in lines[i + 1:] if l.strip()), "")
            if not (list_stack and re.match(r"^\s*([-*+]|\d+[.)])\s+", nxt)):
                close_lists()                       # 空行后接着还是列表项:列表不关(松散列表)
            i += 1
            continue
        if stripped.startswith(">"):
            flush_para(); close_lists()
            out += [r"\begin{quote}", _inline_to_tex(stripped.lstrip("> ").strip(), num), r"\end{quote}", ""]
            i += 1
            continue
        para.append(stripped)
        i += 1
    flush_para()
    close_lists()
    return "\n".join(out).strip() + "\n"


_BUILTIN_TEMPLATE = r"""\documentclass[11pt]{article}
\usepackage[utf8]{inputenc}
\usepackage[T1]{fontenc}
\usepackage{graphicx,booktabs,amsmath,amssymb,hyperref,xcolor}
\usepackage[numbers]{natbib}
\newcommand{\todo}[1]{\textcolor{red}{\textbf{[#1]}}}
\title{%TITLE%}
\author{%AUTHORS%}
\date{}
\begin{document}
\maketitle
% PAPER_BODY
\bibliographystyle{plainnat}
\bibliography{refs}
\end{document}
"""


def build_latex(chapters: list[Chapter], num: Numbering, tables: dict, out_dir: Path,
                template_dir: Path | None, title: str, authors: list[str],
                bib_path: Path) -> dict:
    """写 latex/ 工程。返回 {"files": [...], "cited": [...], "checks": [...]}。"""
    out_dir.mkdir(parents=True, exist_ok=True)
    sec_dir = out_dir / "sections"
    sec_dir.mkdir(exist_ok=True)
    for old in sec_dir.glob("*.tex"):
        old.unlink()
    inputs, abstract_tex, files = [], "", []
    for idx, ch in enumerate(chapters, start=1):
        body_md, _ = normalize_headings(ch.en, ch.title)
        if not ch.en.strip():
            tex = rf"\todo{{chapter {_escape_plain(ch.title)}: no final text}}" + "\n"
        else:
            tex = md_to_tex(body_md, num, tables)
        if ch.ctype == "abstract":
            abstract_tex = "\\begin{abstract}\n" + tex + "\\end{abstract}\n"
            continue
        name = f"{_slug(ch.folder)}.tex"
        (sec_dir / name).write_text(rf"\section{{{_inline_to_tex(ch.title, num)}}}" + "\n\n" + tex,
                                    encoding="utf-8")
        inputs.append(rf"\input{{sections/{name[:-4]}}}")
        files.append(f"sections/{name}")

    template_main = (template_dir / "main.tex") if template_dir and (template_dir / "main.tex").is_file() else None
    if template_main:
        for p in template_dir.rglob("*"):
            if p.is_file() and p.name != "main.tex":
                dest = out_dir / p.relative_to(template_dir)
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(p.read_bytes())
        tpl = template_main.read_text(encoding="utf-8", errors="replace")
    else:
        tpl = _BUILTIN_TEMPLATE
    if r"\newcommand{\todo}" not in tpl and r"\providecommand{\todo}" not in tpl:
        tpl = tpl.replace(r"\begin{document}", "\\providecommand{\\todo}[1]{\\textbf{[#1]}}\n\\begin{document}", 1)
    tpl = (tpl.replace("%TITLE%", _escape_plain(title))
              .replace("%AUTHORS%", _escape_plain(", ".join(authors) or "Anonymous"))
              .replace("%ABSTRACT%", ""))
    body = "\n".join(inputs)
    block = (abstract_tex + "\n" if abstract_tex else "") + body
    if "% PAPER_BODY" in tpl:
        tpl = tpl.replace("% PAPER_BODY", block, 1)
    else:
        tpl = tpl.replace(r"\end{document}", block + "\n\\end{document}", 1)
    (out_dir / "main.tex").write_text(tpl, encoding="utf-8")
    files.append("main.tex")

    all_tex = list(sec_dir.glob("*.tex")) + [out_dir / "main.tex"]
    cited = sorted(set(k for f in all_tex for k in _cite_keys(f.read_text(encoding="utf-8"))))
    bib_text = bib_entry_text(bib_path, cited)
    (out_dir / "refs.bib").write_text(bib_text, encoding="utf-8")
    files.append("refs.bib")
    # 静态检查对着**输出的** refs.bib(被引条目缺失会在这里暴露)
    return {"files": files, "cited": cited, "checks": static_checks(out_dir, out_dir / "refs.bib")}


_CITE_ANY_RE = re.compile(r"\\(?:paren|text|auto|foot|smart|super)?cite[a-zA-Z]*\*?(?:\[[^\]]*\])*\{([^}]*)\}")


def _cite_keys(tex: str) -> list[str]:
    keys = []
    for m in _CITE_ANY_RE.finditer(tex):
        keys += [k.strip() for k in m.group(1).split(",") if k.strip() and k.strip() != "*"]
    return keys


def static_checks(latex_dir: Path, bib_path: Path) -> list[str]:
    """LaTeX 工程静态检查(不编译)。返回问题清单。"""
    problems = []
    known = set(load_bib(bib_path)[0])
    labels: dict = {}
    for f in sorted(latex_dir.rglob("*.tex")):
        text = f.read_text(encoding="utf-8", errors="replace")
        stripped = re.sub(r"(?<!\\)%.*", "", text)
        if stripped.count("{") != stripped.count("}"):
            # \{ \} 转义不计
            opens = len(re.findall(r"(?<!\\)\{", stripped))
            closes = len(re.findall(r"(?<!\\)\}", stripped))
            if opens != closes:
                problems.append(f"{f.name}: 花括号不配平({opens} 个 {{ / {closes} 个 }})")
        for key in unknown_cite_keys(stripped, known):
            problems.append(f"{f.name}: 引用 key '{key}' 不在 refs.bib 里")
        for lab in re.findall(r"\\label\{([^}]+)\}", stripped):
            if lab in labels:
                problems.append(f"标签 {lab} 重复({labels[lab]} 与 {f.name})")
            labels[lab] = f.name
        for env in set(re.findall(r"\\begin\{([^}]+)\}", stripped)):
            if len(re.findall(rf"\\begin\{{{re.escape(env)}\}}", stripped)) != \
                    len(re.findall(rf"\\end\{{{re.escape(env)}\}}", stripped)):
                problems.append(f"{f.name}: 环境 {env} 的 begin/end 不配对")
    for f in sorted(latex_dir.rglob("*.tex")):
        for ref in re.findall(r"\\ref\{([^}]+)\}", f.read_text(encoding="utf-8", errors="replace")):
            if ref not in labels:
                problems.append(f"{f.name}: \\ref{{{ref}}} 没有对应的 \\label")
    return problems


# ── 待补清单 / 图表清单 / 溯源表 ─────────────────────────────────────────
def collect_markers(text: str) -> list[tuple[str, str]]:
    """正文里的缺口标记 → [(标记, 所在句上下文)]。"""
    found = []
    for m in _MARKER_RE.finditer(text):
        s = max(0, m.start() - 60)
        found.append((m.group(1), text[s:m.end() + 40].replace("\n", " ").strip()))
    for m in _UNKNOWN_RES_RE.finditer(text):
        s = max(0, m.start() - 60)
        found.append(("未知结果", text[s:m.end() + 40].replace("\n", " ").strip()))
    return found


def build_todo_report(chapters: list[Chapter], extra_notes: list[str], num: Numbering,
                      tables: dict) -> str:
    out = ["# 待补清单(程序汇总)", "",
           "初稿允许带缺口,但缺口都在这里。**“未核验”的章节不能当成已通过检查。**", ""]
    if extra_notes:
        out += ["## 材料与流程提醒", ""] + [f"- {n}" for n in extra_notes] + [""]
    unverified = [c for c in chapters if c.status == "unverified"]
    out += ["## 核验状态", ""]
    for c in chapters:
        tag = {"verified": "核验通过", "unverified": "**未核验**"}.get(c.status, "无记录")
        out.append(f"- {c.number}. {c.title}({c.folder}): {tag}" + ("" if c.en.strip() else " — 没有终稿"))
    out.append("")
    figs_cited = set()
    for c in chapters:
        figs_cited |= {m.group(2).strip() for m in _REF_RE.finditer(c.en) if m.group(1) == "FIG"}
    known_tables = {t.name for t in tables.values()}
    for c in chapters:
        items = []
        for reason in c.reasons:
            items.append(f"未核验原因: {reason}")
        for kind, ctx in collect_markers(c.en):
            desc = dict(MARKERS).get(kind, kind)
            items.append(f"[{kind}] {desc}:…{ctx}…")
        for name in rt.unknown_tables(c.en, known_tables):
            items.append(f"[TABLE] 表 `{name}` 在结果目录里不存在")
        for m in _REF_RE.finditer(c.en):
            kind, name = m.group(1), m.group(2).strip()
            if (kind == "TAB" and name not in num.tables) or (kind == "FIG" and name not in num.figures):
                items.append(f"交叉引用 `{m.group(0)}` 找不到对应的{'表' if kind == 'TAB' else '图'}占位符")
        for b in parse_figure_blocks(c.en):
            if b["id"] not in figs_cited:
                items.append(f"图 `{b['id']}` 没有被正文引用")
        for u in c.used:
            if u.get("negative"):
                items.append(f"派生数字 {u['placeholder']} = {u['text']} 为负,检查正文措辞(提升 / 下降)")
        if not c.en.strip():
            items.append("本章没有终稿(流程失败或尚未生成)")
        todo_lines = [ln for ln in c.todo.splitlines() if ln.strip().startswith("-")]
        for ln in todo_lines[:30]:
            if "No outstanding" not in ln:
                items.append("todo.md: " + ln.strip("- ").strip())
        if items:
            out += [f"## {c.number}. {c.title}", ""] + [f"- {x}" for x in dict.fromkeys(items)] + [""]
    if not unverified and not any(ln.startswith("## ") and ln[3:4].isdigit() for ln in out) \
            and all(c.en.strip() for c in chapters):
        out += ["(没有发现缺口)", ""]
    out += ["## 投稿前", "",
            "- 人工核对全部引用与数字(数字溯源表在 `数字溯源表.md`)。",
            "- 按会议要求披露 LLM 的使用;作者对全文内容(含引用)负全责。", ""]
    return "\n".join(out)


def _prompt_for_figure(fig: Figure, tables: dict) -> str:
    f = fig.fields
    kind = (f.get("Type") or "").lower()
    kind_zh = {"bar": "分组柱状图", "line": "折线图", "scatter": "散点图", "heatmap": "热图",
               "diagram": "示意图 / 架构图"}.get(kind, kind or "图")
    lines = [f"请绘制一幅**{kind_zh}**,用于学术论文,风格简洁,黑白打印也可读,色盲安全配色,图内不放标题。",
             f"图题:{fig.title}"]
    if f.get("Note"):
        lines.append(f"图注/要表达的内容:{f['Note']}")
    if f.get("Data"):
        lines.append(f"数据来源:{f['Data']}")
        for t in tables.values():
            if t.is_grid and (t.name in f["Data"]):
                lines.append(f"数据(取自 {t.file},原样):")
                lines.append(rt.render_table_markdown(t))
    if f.get("Source"):
        lines.append(f"设计依据:{f['Source']}(见 idea.md 对应小节)")
    if f.get("Supports"):
        lines.append(f"它要支撑的论断:{f['Supports']}")
    lines.append("输出矢量图(PDF/SVG)或 300dpi 以上位图;坐标轴写清含义和单位。")
    return "\n".join(lines)


def build_figure_report(num: Numbering, tables: dict, chapters: dict) -> str:
    out = ["# 图表清单(程序汇总)", "",
           "图由你按下面的提示词补。画好后按“建议文件名”放进 `latex/figures/`(.pdf 或 .png),",
           "重跑 `--go` / `--assemble` 后占位框会自动换成图;`paper.md` 里的图注块已带编号。", ""]
    if not num.figure_list:
        out.append("(没有图占位符)")
    for fig in num.figure_list:
        out += [f"## Figure {fig.number} — {fig.title}", "",
                f"- 所在章节:{chapters.get(fig.chapter, fig.chapter)}",
                f"- id:`{fig.fid}`;建议文件名:`fig{fig.number}-{_slug(fig.fid)}.pdf`",
                f"- 类型:{fig.fields.get('Type', '?')}", "", "**绘图提示词**:", "", "```",
                _prompt_for_figure(fig, tables), "```", ""]
    return "\n".join(out)


def build_trace_report(chapters: list[Chapter], res_map: dict, sources: dict,
                       tables_used: dict) -> str:
    out = ["# 数字溯源表(程序汇总)", "",
           "正文里每个来自结果表的数字:占位符 → 替换后的值 → CSV 里的来源(文件:行)。", ""]
    any_row = False
    for c in chapters:
        rows = []
        for u in c.used:
            if u["kind"] == "RES":
                rid = u["ids"][0]
                entry = res_map.get(rid, {})
                rows.append((u["placeholder"], u["text"], entry.get("key", "?"), sources.get(entry.get("key", ""), "?")))
            else:
                a, b = (res_map.get(i, {}) for i in u["ids"])
                op = "a − b" if u["kind"] == "DIFF" else "(a − b) / b × 100"
                rows.append((u["placeholder"], u["text"],
                             f"{op};a={a.get('key','?')},b={b.get('key','?')}",
                             f"{sources.get(a.get('key',''), '?')} 与 {sources.get(b.get('key',''), '?')}"))
        if rows:
            any_row = True
            out += [f"## {c.number}. {c.title}", "", "| 占位符 | 正文里的值 | 结果键 / 算法 | 来源 |", "|---|---|---|---|"]
            for ph, val, key, src in rows:
                out.append(f"| `{ph}` | {val} | {key} | {src} |")
            out.append("")
    if tables_used:
        out += ["## 整表渲染", "", "| 表 | 来源文件 |", "|---|---|"]
        for name, file in tables_used.items():
            out.append(f"| Table(`{name}`) | {file} |")
        out.append("")
        any_row = True
    if not any_row:
        out.append("(正文里没有来自结果表的数字)")
    return "\n".join(out) + "\n"
