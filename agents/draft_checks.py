"""两道确定性的初稿质量检查(不调模型,只用标准库)。

1. 结构检查(structure-check.md):brief 里的每个小节在草稿里有没有、写了多少词。
   字数目标此前只出现在提示词里,没有任何检查;漏写一节或把 200 词的摘要写成 800 词
   都不会被发现。结果交给评审:缺节 = MUST FIX,字数偏离 = SHOULD FIX(绝不靠注水凑数)。
2. 润色保真检查(polish-check.md):Stage 4 由另一个模型整篇重写做"轻润色",提示词要求
   不许改引用/数字/标记,但之前没有任何东西核对它真的没丢。这里比对 draft-v2.md 与
   final.md 的引用 key、占位符、表/图占位、缺口标记、小节标题与字数。

两者都是提示性质的程序产物:每次重算、不进阶段指纹。结构检查只给评审看;润色检查
发现丢失时会让本章标为"未核验"(丢了一条引用或一个数字占位符不是风格问题)。
"""
import os
import re
from collections import Counter
from pathlib import Path

STRUCTURE_CHECK = "structure-check.md"
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


# ── 1. 结构检查 ───────────────────────────────────────────────────────────
def check_structure(text: str, sections: list[dict]) -> list[str]:
    """每个 brief 小节一行结论。sections 来自 parse_brief_sections。

    行首标签:MISSING(找不到这一节的标题)、UNDERLENGTH(不到目标一半)、SHORT(不到 70%)、
    LONG(超出 150%)、OK。真实测试里各节普遍只写到目标的 10%~60%,而提示词里的目标字数
    没有任何检查,评审也没提——所以"不到一半"要能进 MUST FIX。
    只有一个小节的章(摘要、结论等短章)不要求有小节标题,直接数全文。
    容差故意放宽(<70% 或 >150% 才报):起草提示词给的区间是 85%-120%,这里只抓明显跑偏。
    """
    body = body_text(text)
    if not sections:
        return []
    if len(sections) == 1:
        spans = {0: body}
    else:
        lines = body.splitlines()
        heads = _headings(body)
        starts: dict[int, int] = {}
        for idx, section in enumerate(sections):
            want = _norm_title(section.get("title", ""))
            for line_no, have in heads:
                if line_no not in starts.values() and _title_matches(want, have):
                    starts[idx] = line_no
                    break
        order = sorted(starts.values())
        spans = {}
        for idx, line_no in starts.items():
            later = [n for n in order if n > line_no]
            end = later[0] if later else len(lines)
            spans[idx] = "\n".join(lines[line_no + 1:end])
    out = []
    for idx, section in enumerate(sections):
        title = section.get("title", f"section {idx + 1}")
        target = int(section.get("target_words") or 0)
        if idx not in spans:
            out.append(f"MISSING: section \"{title}\" (target ~{target} words) has no heading "
                       f"in the draft")
            continue
        words = count_words(spans[idx])
        tag = "OK"
        if target and words < target * 0.5:
            tag = "UNDERLENGTH"
        elif target and words < target * 0.7:
            tag = "SHORT"
        elif target and words > target * 1.5 + 30:
            tag = "LONG"
        out.append(f"{tag}: section \"{title}\" — {words} words (target ~{target})")
    return out


def write_structure_check(folder_path: str, source_name: str, sections: list[dict]) -> list[str]:
    """读本章 source_name,写 structure-check.md(覆盖)。返回非 OK 的行。"""
    source = Path(folder_path) / source_name
    if not source.is_file() or not sections:
        return []
    rows = check_structure(source.read_text(encoding="utf-8", errors="replace"), sections)
    hits = [r for r in rows if not r.startswith("OK")]
    status = "OK" if not hits else f"{len(hits)} finding(s)"
    lines = [f"# Structure check ({source_name} vs brief.md)",
             f"Status: {status}", "",
             "Deterministic: section presence by heading, word counts by a rough tokenizer. "
             "A renamed heading shows up as MISSING — check before acting.", ""]
    lines += [f"- [STRUCTURE] {r}" for r in rows]
    _atomic_write(Path(folder_path) / STRUCTURE_CHECK, "\n".join(lines) + "\n")
    return hits


# ── 2. 润色保真检查 ───────────────────────────────────────────────────────
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
