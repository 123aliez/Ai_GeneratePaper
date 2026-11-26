"""定稿润色保真检查(polish-check.md,不调模型,只用标准库)。

Stage 4 由另一个模型整篇重写做"轻润色",提示词要求不许改引用/数字/标记,但之前没有
任何东西核对它真的没丢。这里比对 draft-v2.md 与 final.md(及 final.zh.md)的引用 key、
占位符、表/图占位、缺口标记、小节标题与字数。丢引用/占位符/缺口标记会让本章标为
"未核验";标题改名、字数漂移只作提示。每次定稿后重算,不进阶段指纹。
"""
import os
import re
from collections import Counter
from pathlib import Path

# 提示级发现(标题改名、字数漂移)的前缀:写进 polish-check.md 给作者看,但不判未核验——
# 润色微调标题措辞是合理的;丢引用/占位符/缺口标记才是交付物与核验稿不一致。
ADVISORY = "advisory: "
POLISH_CHECK = "polish-check.md"

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$")
_LEADING_NUM_RE = re.compile(r"^\s*(?:\d+(?:\.\d+)*[.)、]?|[IVXivx]+\.)\s+")
_TAIL_HEADING_RE = re.compile(
    r"^#{1,6}\s+(?:\d+(?:\.\d+)*[.)]?\s+)?(?:Limitations of This Draft|Revision (?:Notes|Rationale)|"
    r"Change (?:Log|Notes)|Rationale for (?:Major )?Changes)\b", re.I | re.M)
_FENCE_RE = re.compile(r"```.*?```", re.S)
_CITE_RE = re.compile(r"\\cite[a-zA-Z]*\*?(?:\[[^\]]*\])*\{([^}]*)\}")
_PLACEHOLDER_RE = re.compile(r"\{\{[^{}\n]+\}\}")
_TABLE_RE = re.compile(r"\[TABLE:\s*([^|\]\n]+)")
_FIGURE_RE = re.compile(r"\[FIGURE\s+([^:\]\n]+)")
_XREF_RE = re.compile(r"\[\[[A-Z]+:[^\]\n]+\]\]")
_GAP_MARKERS = ("[MISSING DATA]", "[IDEA NEEDED]", "[DESIGN DETAIL NEEDED]",
                "[CITATION NEEDED]", "[MISSING INFO]", "[undisclosed]", "[FROM CODE")
_WORD_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9'’\-]*")


# ── 共用 ─────────────────────────────────────────────────────────────────
def body_text(text: str) -> str:
    """去掉草稿末尾的 "Limitations of This Draft" / 修订说明等非正文尾巴。"""
    m = _TAIL_HEADING_RE.search(text or "")
    return (text or "")[:m.start()] if m else (text or "")


_ABBREVIATION_RE = re.compile(r"\b(?:e\.g|i\.e|et al|Fig|Eq|Sec|vs|Dr)\.", re.I)
_PROOF_RE = re.compile(
    r"\\begin\{(?:proof|lemma|theorem|corollary)\}|"
    r"^[ \t]*(?:#{1,6}\s+)?(?:\*{1,2})?(?:Proof(?:[.:\s]|$)|"
    r"(?:Lemma|Theorem|Corollary)\s+\d|(?:Gradient |Algebraic )?Derivation\b)",
    re.I | re.M,
)


def methodology_findings(text: str, types: list[str]) -> list[str]:
    """只对正文作提示。梯度符号本身也是合法更新式的一部分。"""
    body = _FENCE_RE.sub("", body_text(text)).strip()
    findings = []
    for match in _PROOF_RE.finditer(body):
        findings.append(f'Check for a proof or derivation at "{match.group().strip()}". '
                        "Keep only the working equation and its intuition.")
    if len(types) != 1 or types[0] not in {"abstract", "intro", "conclusion"}:
        return findings
    chapter_type = types[0]
    body = re.sub(r"^#{1,6}[ \t]+(?:\d+\.?\s+)?(?:Abstract|Introduction|Conclusion)[ \t]*\n?",
                  "", body, count=1, flags=re.I).strip()
    if re.search(r"^#{1,6}\s+\S", body, re.M):
        findings.append(f"Subheading in {chapter_type}: use continuous prose.")
    if chapter_type == "abstract":
        if re.search(r"\n[ \t]*\n", body):
            findings.append("Abstract must be a single paragraph.")
        prose = _ABBREVIATION_RE.sub(lambda m: m.group().replace(".", ""), body)
        sentences = re.split(r'(?<=[.!?])["”)]*(?:\s+|$)', prose)
        count = sum(bool(part.strip()) for part in sentences)
        if count > 7:
            findings.append(f"Abstract has {count} sentences. Compress to at most 7.")
    return findings


def count_words(text: str) -> int:
    """正文词数的粗估:去掉围栏块、表格行、占位符行、引用命令后数英文词。"""
    text = _FENCE_RE.sub(" ", text or "")
    lines = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("|") or stripped.startswith("[TABLE:") or stripped.startswith("[FIGURE"):
            continue
        if _HEADING_RE.match(stripped):
            continue
        lines.append(line)
    text = "\n".join(lines)
    text = _CITE_RE.sub(" ", text)
    text = _PLACEHOLDER_RE.sub(" NUM ", text)
    return len(_WORD_RE.findall(text))


def _norm_title(title: str) -> str:
    title = re.sub(r"[*_`]", "", title or "")
    title = _LEADING_NUM_RE.sub("", title)
    return re.sub(r"[^a-z0-9\u4e00-\u9fff]+", " ", title.lower()).strip()


def _headings(text: str) -> list[tuple[int, str]]:
    """[(行号, 规范化标题)],跳过围栏块里的 `#` 行。"""
    out, in_fence = [], False
    for i, line in enumerate((text or "").splitlines()):
        if line.strip().startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        m = _HEADING_RE.match(line)
        if m:
            out.append((i, _norm_title(m.group(2))))
    return out


def _title_matches(want: str, have: str) -> bool:
    if not want or not have:
        return False
    return want == have or want in have or (len(have) >= 6 and have in want)


def _atomic_write(path: Path, text: str) -> None:
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


# ── 润色保真检查 ───────────────────────────────────────────────────────
def _cite_keys(text: str) -> set[str]:
    keys = set()
    for m in _CITE_RE.finditer(text or ""):
        keys |= {k.strip() for k in m.group(1).split(",") if k.strip()}
    return keys


def _counter_diff(before: Counter, after: Counter) -> tuple[list[str], list[str]]:
    lost = sorted((before - after).elements())
    added = sorted((after - before).elements())
    return lost, added


def _short(items: list[str], limit: int = 8) -> str:
    items = list(dict.fromkeys(items))
    more = f" (+{len(items) - limit} more)" if len(items) > limit else ""
    return ", ".join(items[:limit]) + more


def polish_findings(base: str, final: str, *, check_length: bool = True,
                    check_headings: bool = True, label: str = "final.md") -> list[str]:
    """base(核验过的修订稿)→ final(润色稿)丢了/多了什么。空列表 = 保真。"""
    base, final = body_text(base), body_text(final)
    out = []
    lost = sorted(_cite_keys(base) - _cite_keys(final))
    added = sorted(_cite_keys(final) - _cite_keys(base))
    if lost:
        out.append(f"{label} dropped citation key(s): {_short(lost)}")
    if added:
        out.append(f"{label} added citation key(s) absent from its source text: {_short(added)}")
    for name, regex in (("number placeholder(s)", _PLACEHOLDER_RE),
                        ("cross-reference placeholder(s)", _XREF_RE)):
        lost, added = _counter_diff(Counter(regex.findall(base)), Counter(regex.findall(final)))
        if lost:
            out.append(f"{label} dropped {name}: {_short(lost)}")
        if added:
            out.append(f"{label} added {name}: {_short(added)}")
    for name, regex in (("[TABLE] placeholder(s)", _TABLE_RE), ("[FIGURE] block(s)", _FIGURE_RE)):
        b = Counter(x.strip() for x in regex.findall(base))
        f = Counter(x.strip() for x in regex.findall(final))
        lost, added = _counter_diff(b, f)
        if lost:
            out.append(f"{label} dropped {name}: {_short(lost)}")
        if added:
            out.append(f"{label} added {name}: {_short(added)}")
    for marker in _GAP_MARKERS:
        nb, nf = base.count(marker), final.count(marker)
        if nf < nb:
            out.append(f"{label} removed {nb - nf} gap marker(s) {marker}"
                       f"{']' if not marker.endswith(']') else ''} — a gap cannot be closed by polishing")
    if check_headings:
        have = [h for _, h in _headings(final)]
        gone = [h for _, h in _headings(base)
                if h and not any(_title_matches(h, x) for x in have)]
        if gone:
            out.append(f"{ADVISORY}{label} dropped or renamed section heading(s): {_short(gone)}")
    if check_length:
        wb, wf = count_words(base), count_words(final)
        if wb >= 80 and not (0.85 <= wf / wb <= 1.15):
            out.append(f"{ADVISORY}{label} length changed {wb} → {wf} words "
                       f"({wf / wb:.0%} of the verified draft); a light polish should stay within ±15%")
    return out


def write_polish_check(folder_path: str, base_name: str = "draft-v2.md") -> list[str]:
    """比对 base_name 与 final.md / final.zh.md,写 polish-check.md。返回发现列表。

    必须在 RES 占位符被替换成数值**之前**调用(替换后 final 里已没有占位符可比)。
    中文版只比引用/占位符/标记(词数与标题是另一种语言,不可比)。
    """
    folder = Path(folder_path)
    base, final = folder / base_name, folder / "final.md"
    if not base.is_file() or not final.is_file():
        return []
    base_text = base.read_text(encoding="utf-8", errors="replace")
    final_text = final.read_text(encoding="utf-8", errors="replace")
    findings = polish_findings(base_text, final_text)
    zh = folder / "final.zh.md"
    if zh.is_file():
        findings += polish_findings(final_text, zh.read_text(encoding="utf-8", errors="replace"),
                                    check_length=False, check_headings=False,
                                    label="final.zh.md (vs final.md)")
    hard = [x for x in findings if not x.startswith(ADVISORY)]
    status = ("OK" if not findings else
              f"LOSS — {len(hard)} finding(s)" if hard else f"OK with {len(findings)} advisory note(s)")
    lines = [f"# Polish fidelity check (final.md vs {base_name})",
             f"Status: {status}", "",
             "Finalization may only polish wording. Citations, number placeholders, table/figure "
             "placeholders, gap markers and section headings must survive unchanged.", ""]
    lines += ([f"- [POLISH-ADVISORY] {x[len(ADVISORY):]}" if x.startswith(ADVISORY)
               else f"- [POLISH] {x}" for x in findings] if findings else ["No differences found."])
    _atomic_write(folder / POLISH_CHECK, "\n".join(lines) + "\n")
    return hard


def read_polish_findings(folder_path: str) -> list[str]:
    """读已有 polish-check.md 里的硬发现(续跑时 final 已被替换过数值,不能重算)。
    提示级(POLISH-ADVISORY)不返回。"""
    path = Path(folder_path) / POLISH_CHECK
    if not path.is_file():
        return []
    return [ln[len("- [POLISH] "):].strip()
            for ln in path.read_text(encoding="utf-8", errors="replace").splitlines()
            if ln.startswith("- [POLISH] ")]


def manuscript_findings(text, tables, bib_keys, *, result_prose=True, english=True):
    """合并结构化图表的硬检查，供起草、修订与终稿核验共用。"""
    from .assemble import parse_figure_blocks
    from .results_tables import table_style_warnings, recover_table_layouts

    body = recover_table_layouts(body_text(text), tables)
    findings = table_style_warnings(body, tables, result_prose=result_prose, english=english)
    for fig in parse_figure_blocks(body):
        note = fig["fields"].get("Note", "")
        for group in re.findall(r"(?<!\[)\[([A-Za-z][\w:./,+ -]*)\](?!\])", note):
            keys = [k.strip() for k in group.split(",")]
            if any(k in bib_keys or re.search(r"[a-zA-Z]\w*(?:19|20)\d{2}\w*", k) for k in keys):
                findings.append(f"Figure {fig['id']}: replace bare [{group}] in Note with a verified BibTeX citation.")
    return findings


def required_citation_findings(text: str, required) -> list[str]:
    """核对实际引用命令和分配小节，围栏代码与正文尾注不算引用。"""
    clean = _FENCE_RE.sub("", body_text(text))
    lines = clean.splitlines()
    headings = []
    for index, line in enumerate(lines):
        match = _HEADING_RE.match(line.strip())
        if match:
            headings.append((index, len(match[1]), _norm_title(match[2])))
    findings = []
    for row in required:
        section_text = clean
        if row.get("section_count", 1) > 1:
            wanted = _norm_title(row.get("section_title", ""))
            matches = [(i, depth) for i, depth, title in headings if _title_matches(wanted, title)]
            if matches:
                start, level = matches[0]
                end = next((i for i, depth, _ in headings if i > start and depth <= level), len(lines))
                section_text = "\n".join(lines[start + 1:end])
            else:
                section_text = ""
        if row["key"] not in _cite_keys(section_text):
            findings.append(f"[MISSING-CITATION] {row['key']} (section {row['section']}): {row['role']}")
    return findings
