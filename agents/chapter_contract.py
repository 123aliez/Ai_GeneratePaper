"""章节契约:每种章节类型写什么、文献怎么用、禁止写什么。

契约文本是单一来源。起草侧、评审侧、备料侧三种子句都从同一份 CONTRACTS 生成,
注入每个改写正文的阶段。契约粗检(check_contract)只提示、不拦截,也不改变核验状态。

只依赖 chapter_type。提示词英文、注释中文。含 {{RES-xxx}} 的子句一律用 f-string
拼接字典值,不得对整串调用 .format()——否则双花括号会被吃掉一层。
"""

from __future__ import annotations

import os
import re
from pathlib import Path

from .chapter_type import normalize_type

CONTRACTS: dict[str, dict[str, str]] = {
    "abstract": {
        "focus": "Summarize OUR problem, OUR method, OUR headline results (as {{RES-xxx}} "
                 "placeholders) and OUR contributions from idea.md section 6.",
        "references": "Cite nothing: an abstract contains no \\cite.",
        "forbidden": "Describing other papers; any contribution not listed in idea.md section 6.",
    },
    "intro": {
        "focus": "OUR research background, the scientific problem and OUR motivation "
                 "(idea.md sections 1-2), ending with the contribution list of idea.md section 6.",
        "references": "Cite prior work only to show that the problem matters and is not yet solved: "
                      "at most one clause per cited work and no paragraph about a single paper. "
                      "State the gap in at most one sentence taken from idea.md section 2; the "
                      "detailed comparison with the closest works belongs to the related-work chapter.",
        "forbidden": "Explaining another paper's method in detail; numbers reported by other papers; "
                     "any contribution not listed in idea.md section 6.",
    },
    "related": {
        "focus": "Prior research grouped by theme: how each line of work approaches the problem "
                 "and where it falls short, then how OUR work differs from the closest works.",
        "references": "A-level papers: approach and limitation in at most three sentences each. "
                      "B-level papers: cited as members of a theme with at most one clause each. "
                      "C-level papers: citation only. The difference between our work and any prior "
                      "work comes ONLY from idea.md section 5 - never derive or invent a difference "
                      "from the notes.",
        "forbidden": "Details of our own method (they belong to the method chapter); numbers "
                     "reported by other papers; judging whether our contribution is novel.",
    },
    "background": {
        "focus": "The base concepts and notation a reader needs before the method chapter.",
        "references": "Cite the source of each base concept and describe the concept itself, not "
                      "the cited paper's own contribution story.",
        "forbidden": "Comparing prior work with our method; claiming novelty.",
    },
    "method": {
        "focus": "OUR idea in full: core insight, architecture, every formula and the training "
                 "objective, from idea.md sections 3-4.",
        "references": "Prior work may appear ONLY as attribution of a base technique we adopt: one "
                      "citation plus at most one clause, e.g. 'We use X \\cite{x} as the image "
                      "encoder.'",
        "forbidden": "Comparative wording about cited work (unlike, in contrast to, compared with, "
                     "different from, outperforms, prior/previous/existing methods); describing "
                     "another paper's innovations or motivation; positioning against prior work "
                     "(it belongs to the related-work chapter); result numbers.",
    },
    "experiments": {
        "focus": "OUR experimental design: datasets, protocol, baselines, metrics and "
                 "implementation details, from the experiment settings file.",
        "references": "Cite datasets, baselines and metric definitions; describe each baseline in "
                      "at most one sentence.",
        "forbidden": "Re-explaining our method; numbers reported by other papers unless they are "
                     "in the result tables.",
    },
    "results": {
        "focus": "Objective analysis of OUR comparison and ablation results; every number is a "
                 "placeholder or a rendered table.",
        "references": "Cite only to identify a dataset or a baseline.",
        "forbidden": "Claims stronger than the data; re-explaining the method; explaining an "
                     "outcome through another paper's mechanism in detail.",
    },
    "discussion": {
        "focus": "What OUR findings mean, the conditions under which OUR method applies, and "
                 "its limitations (idea.md section 7).",
        "references": "Cite only briefly, to place a finding in context.",
        "forbidden": "New results or new contributions; an extended review of other work.",
    },
    "conclusion": {
        "focus": "OUR core method and the contributions that the experiments of THIS paper support.",
        "references": "Cite nothing: a conclusion contains no \\cite.",
        "forbidden": "Stating a contribution from idea.md section 6 as demonstrated when no result "
                     "in the data index supports it (hedge it or leave it out, and list it in "
                     "todo.md); any new claim.",
    },
}
# 共享契约的类型:同一个 dict 对象,不复制文本。
CONTRACTS["theory"] = CONTRACTS["method"]
CONTRACTS["analysis"] = CONTRACTS["results"]
CONTRACTS["ablation"] = CONTRACTS["results"]
CONTRACTS["limitations"] = CONTRACTS["discussion"]

DEFAULT_CONTRACT = {
    "focus": "OUR own work as stated in idea.md and the result tables.",
    "references": "Cite to support factual statements; at most one clause per cited work.",
    "forbidden": "Extended descriptions of other papers; judging whether our contribution is novel.",
}

NOVELTY_GUARD = ("Do NOT raise MUST FIX because the contribution seems not novel or seems to "
                 "overlap prior work: novelty is the author's responsibility, not the reviewer's.")

# 契约粗检只覆盖方法/理论(对比措辞紧挨引用)和摘要/结论(不许 \cite)。
_METHOD_TYPES = {"method", "theory"}
_NO_CITE_TYPES = {"abstract", "conclusion"}

COMPARISON_RE = re.compile(
    r"\b(unlike|in contrast (?:to|with)|compared (?:to|with)|different from|differs? from|"
    r"outperform\w*|superior to|(?:prior|previous|existing) (?:work|works|methods?|approaches?))\b",
    re.IGNORECASE)
CITE_RE = re.compile(r"\\cite[pt]?\*?\{[^}]*\}")

_FENCE_RE = re.compile(r"```.*?```", re.DOTALL)
_MATH_RE = re.compile(r"\$\$.*?\$\$", re.DOTALL)
_SENTENCE_RE = re.compile(r"(?<=[.!?])\s+")


def contract_for(chapter_type: str) -> dict:
    """规范化后查 CONTRACTS;查不到(含 unknown)返回 DEFAULT_CONTRACT。"""
    return CONTRACTS.get(normalize_type(chapter_type), DEFAULT_CONTRACT)


def scope_types(route: dict) -> list[str]:
    """本章类型集合:section_types 按小节编号升序取值、按首次出现去重。

    section_types 为空时返回 [route 的章级 type](缺省 unknown)。
    """
    section_types = route.get("section_types") or {}
    if not section_types:
        return [route.get("type", "unknown")]
    seen: list[str] = []
    for number in sorted(section_types, key=lambda n: (isinstance(n, str), n)):
        t = section_types[number]
        if t not in seen:
            seen.append(t)
    return seen


def _one(prefix: str, chapter_type: str) -> str:
    c = contract_for(chapter_type)
    return (f"{prefix} ({chapter_type}): Focus - {c['focus']} References - {c['references']} "
            f"Forbidden - {c['forbidden']}")


def build_contract_clause(route: dict) -> str:
    """起草侧。单一类型一段;多类型先说明各段按自己的小节适用,同类型只出一段。"""
    types = scope_types(route)
    if len(types) == 1:
        return _one("CHAPTER CONTRACT", types[0]) + "\n\n"
    section_types = route.get("section_types") or {}
    order = sorted(section_types, key=lambda n: (isinstance(n, str), n))
    mapping = ", ".join(f"section {n} is {section_types[n]}" for n in order)
    head = ("This run covers several section types; apply each contract to its own "
            f"sections only: {mapping}")
    body = "\n".join(_one("CHAPTER CONTRACT", t) for t in types)
    return head + "\n" + body + "\n\n"


def build_contract_review_clause(route: dict) -> str:
    """评审侧。每个类型一段(违约即 MUST FIX),全部类型之后追加一次 NOVELTY_GUARD。"""
    parts = []
    for t in scope_types(route):
        c = contract_for(t)
        parts.append(
            f"CHAPTER CONTRACT CHECK ({t}): Raise MUST FIX when the chapter violates this "
            f"contract. Focus - {c['focus']} References - {c['references']} "
            f"Forbidden - {c['forbidden']}")
    parts.append(NOVELTY_GUARD)
    return "\n".join(parts) + "\n\n"


def build_contract_reference_rule(route: dict) -> str:
    """备料侧(Stage 0a)。每个类型的文献规则用分号连接。"""
    rules = "; ".join(f"{t}: {contract_for(t)['references']}" for t in scope_types(route))
    return f"REFERENCE RULES FOR THIS CHAPTER: {rules}\n\n"


def _visible_text(text: str) -> str:
    """去掉围栏代码块与 $$…$$ 公式块:里面的对比词和引用不算正文。"""
    text = _FENCE_RE.sub(" ", text or "")
    return _MATH_RE.sub(" ", text)


def check_contract(text: str, types: list[str]) -> list[str]:
    """返回提示行(不含前缀 "- [CONTRACT] ")。

    多类型直接跳过(各段适用的契约不同,整章粗检会误报)。方法/理论章只报
    "对比措辞紧挨引用"的句子;摘要/结论章只报含 \\cite 的句子。其余类型不报。
    """
    if len(types) != 1:
        return ["skipped: this run covers several section types"]
    chapter_type = types[0]
    if chapter_type not in _METHOD_TYPES and chapter_type not in _NO_CITE_TYPES:
        return []
    hits = []
    for sentence in _SENTENCE_RE.split(_visible_text(text).strip()):
        sentence = sentence.strip()
        if not sentence:
            continue
        if chapter_type in _METHOD_TYPES and CITE_RE.search(sentence) and COMPARISON_RE.search(sentence):
            hits.append(f'comparative wording next to a citation in a {chapter_type} chapter: '
                        f'"{sentence[:160]}"')
        elif chapter_type in _NO_CITE_TYPES and CITE_RE.search(sentence):
            hits.append(f'citation in a {chapter_type} chapter: "{sentence[:160]}"')
    return hits


def write_contract_check(folder_path: str, source_name: str, types: list[str]) -> list[str]:
    """读本章 source_name,写 contract-check.md(覆盖)。源文件不存在则不写文件。

    每次都重算,不进阶段指纹。返回提示行(不含前缀)。
    """
    source = Path(folder_path) / source_name
    if not source.is_file():
        return []
    hits = check_contract(source.read_text(encoding="utf-8", errors="replace"), types)
    status = "OK" if not hits else f"{len(hits)} hint(s) - judge each, false positives possible"
    lines = [f"# Chapter contract check ({source_name})",
             f"Types: {', '.join(types)}",
             f"Status: {status}",
             ""]
    # 提示里的句子可能含换行:折叠成一行再落盘,否则 load_chapters 按行读会丢正文。
    lines += [f"- [CONTRACT] {' '.join(hit.splitlines())}" for hit in hits] if hits else ["No hints."]
    out = Path(folder_path) / "contract-check.md"
    tmp = out.with_name(f".{out.name}.{os.getpid()}.tmp")
    tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
    os.replace(tmp, out)
    return hits
