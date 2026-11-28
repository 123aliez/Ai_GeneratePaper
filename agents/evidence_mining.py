"""起草前的证据挖掘(STORM 的多视角问答,适配实验论文)。

起草一节前,多个评审"视角"对当前内容源提硬问题,每个都从证据作答,且强制一句
"I cannot answer from the provided evidence" 兜底(STORM 的 AnswerQuestion fallback),
杜绝为填空而编造。问答收进 evidence-pack,喂给规划者与起草者——这正是阻止
outline→正文 变空壳的那一步。

orchestrator 在 Stage 1 之前调 `run_evidence_mining(...)`,写出 `evidence-pack.md`,
存在即跳过(断点续跑)。

证据来源不再是任何聚合文件:idea 全文每个 stage 的提示词第一行直接指向全局 idea.md,
这里只把 data 类章节导航用的 data-index.md 一并点给 Agent,idea + (data 类)data-index
即证据边界。
"""
import os
from pathlib import Path

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from config import IDEA_PATH, DATA_ROOT
from .materials import ALL_NOTES_BUDGET
from .paths import data_index_path
from .chapter_type import IDEA, DATA, MIXED

# 视角随模式与章节取材源不同。用"统计显著性如何?"去审方法章,会产出它永远不会报告
# 的统计问题;用"为什么这个设计是对的?"去审结果章,会产出它无法从数字回答的问题。
# 每个取材源拿"自己的证据真正能回答的"那组问题。
IDEA_PERSPECTIVES = [
    ("Positioning reviewer",
     "Using ONLY idea.md section 5, which prior works must this chapter distinguish the "
     "contribution from, and what difference does the author state? Do not judge whether "
     "the contribution is novel."),
    ("Mechanism reviewer", "WHY should this work? What is the causal story from design choice to expected effect?"),
    ("Precision reviewer", "Which definitions, assumptions and implementation details are essential to understand and reproduce the core mechanism, and which are routine supplementary details?"),
    ("Design-alternative reviewer", "Which central design choices need a rationale to understand the contribution, and what rationale does the author actually give? Do not inventory hypothetical alternatives."),
]
# 方法/理论章不问"和谁比"。只问采用了哪些基础技术、各自能否用一句引用交代清楚。
METHOD_PERSPECTIVES = [
    ("Attribution reviewer",
     "Which base techniques does the design adopt from prior work, and can each be credited "
     "with one citation and one clause without describing that work's own innovations?"),
    IDEA_PERSPECTIVES[1],   # Mechanism reviewer
    IDEA_PERSPECTIVES[2],   # Precision reviewer
    IDEA_PERSPECTIVES[3],   # Design-alternative reviewer
]
# 相关工作/背景章的主材料是文献笔记,不是 idea 的机制。原先它们按 idea 族拿到
# Mechanism / Precision / Design-alternative 视角、答案只许锚定 idea.md——没有一个视角去读
# 笔记,证据包里全是"我们的设计为什么对",真实测试里相关工作章因此只写出三句话。
# Positioning 仍排第一:与前人工作的差异只能来自 idea.md 第 5 节,不从笔记推导。
LITERATURE_PERSPECTIVES = [
    IDEA_PERSPECTIVES[0],   # Positioning reviewer
    ("Thematic-grouping reviewer",
     "Into which lines of work do the papers selected in input.md fall, which keys belong to "
     "each line, and what single idea ties a line together according to the notes?"),
    ("Approach-and-limitation reviewer",
     "For each A-level and B-level paper selected in input.md: what does its note say the "
     "approach is, what problem it addresses, and which limitation does the NOTE itself state? "
     "Never infer a limitation the note does not state."),
    ("Coverage reviewer",
     "Which brief.md sections or bullets have no selected paper behind them, and which "
     "selected papers have no note (so they can be cited but not described)?"),
]
BACKGROUND_PERSPECTIVES = [
    ("Concept-source reviewer",
     "Which base concepts and notation does the method (idea.md sections 3-4) rely on, and "
     "which selected paper's note is the source of each?"),
    ("Definition reviewer",
     "How does the source note define each concept (symbols, assumptions)? Quote the note; "
     "do not complete a definition from memory."),
    LITERATURE_PERSPECTIVES[3],   # Coverage reviewer
]
DATA_PERSPECTIVES = [
    ("Headline-result reviewer", "Which comparisons answer the central research question, what do they establish, and which negative results or exceptions materially constrain the finding?"),
    ("Baseline reviewer", "Against which baselines is the gain shown, and what do the matched comparisons demonstrate?"),
    ("Setup reviewer", "Which setup details (data, protocol, hyperparameters) does a reader need to trust these comparisons?"),
    ("Over-claim reviewer", "Which stated conclusion is stronger than the numbers support, so it must be phrased more precisely?"),
]
MIXED_PERSPECTIVES = [
    ("Claim-evidence reviewer", "Which established findings support the interpretation of the contribution? Identify their support internally; do not require each prose claim to repeat a number."),
    ("Mechanism reviewer", "Do the results actually confirm the proposed mechanism, or only correlate with it?"),
    ("Over-claim reviewer", "Which stated conclusion is stronger than the numbers support?"),
    ("Takeaway reviewer", "Which result best supports the contribution, and what does it mean for the reader?"),
]
# 兼容别名:family 路由出现前的实验默认。
EXPERIMENT_PERSPECTIVES = DATA_PERSPECTIVES

_FAMILY_PERSPECTIVES = {
    IDEA: IDEA_PERSPECTIVES,
    DATA: DATA_PERSPECTIVES,
    MIXED: MIXED_PERSPECTIVES,
}


def perspectives_for_mode(family: str = DATA) -> list[tuple[str, str]]:
    return _FAMILY_PERSPECTIVES.get(family, MIXED_PERSPECTIVES)


def perspectives_for_route(family: str, types: list[str]) -> list[tuple[str, str]]:
    """本章覆盖的类型全部是方法/理论时用 Attribution 视角;否则按 family。"""
    if types and set(types) <= {"method", "theory"}:
        return METHOD_PERSPECTIVES
    if types and set(types) <= {"background"}:
        return BACKGROUND_PERSPECTIVES
    if _is_literature(types):
        return LITERATURE_PERSPECTIVES
    return perspectives_for_mode(family)


def _is_literature(types: list[str] | None) -> bool:
    """本次运行覆盖的类型全部是相关工作/背景 → 证据来自文献笔记。"""
    return bool(types) and set(types) <= {"related", "background"}


_LITERATURE_GROUNDING = (
    "Ground it in the author's NOTE on a specific paper (the attached note:<key> documents of "
    "the papers selected in input.md) and name the key next to every statement about that "
    "paper. A C-level paper has only its one-line summary in ref-index.md — say nothing beyond "
    "it. Statements about OUR work, and every difference between our work and prior work, are "
    "grounded in a quoted passage of idea.md (section 5 for differences) — never derived from "
    "the notes.")


# "把答案锚定在证据上"对每个取材源含义不同:idea 答案锚定在作者自己的设计陈述,
# data 答案锚定在记录下来的某个数字。idea 现在是每个 stage 提示词第一行就指向的
# 全局文件,所以指令说"读 idea.md",而不是去找某个 pack 里的小节标题。
_GROUNDING_RULE = {
    IDEA: ("Ground it in a specific passage of idea.md (attached) — quote the "
           "author's own wording for the design or claim you are relying on. A number "
           "from data-index.md is acceptable only as motivation, never as the "
           "explanation of why the design works."),
    DATA: ("Ground it in a specific value from data-index.md (the program-generated data "
           "index), citing its `{{RES-xxx}}` id and the table/row/column. Never derive a "
           "value the index does not contain."),
    MIXED: ("Ground it in either a quoted passage of idea.md or a specific value from "
            "data-index.md — and say which."),
}


def _evidence_boundary(family: str, types: list[str] | None = None) -> str:
    """一句话点明本节的证据边界:idea 是全局最高优先输入;data 类额外读 data-index;
    相关工作/背景章以本章选定文献的笔记为主证据。"""
    if _is_literature(types):
        return (
            "The document idea.md (attached) states OUR contribution and (section 5) how it "
            "differs from prior work. input.md (attached) lists the references selected for this "
            "chapter; the attached note:<key> documents are the author's notes on them. The notes "
            "are the evidence for everything said about prior work; idea.md is the evidence for "
            "everything said about ours.")
    idea_line = (
        "The document idea.md (attached) is the author's own statement of the contribution "
        "(novelty, mechanism, design). It is the highest-priority input for every "
        "section; never restate it from memory, never paraphrase the claim.")
    if family in (DATA, MIXED):
        data_line = (
            " The document data-index.md (attached) is the program-generated data index "
            "(one grid per result table, cells shown as `{{RES-xxx}}=value`). Every number "
            "you cite must trace to a cell there.")
        return idea_line + data_line
    return idea_line


def build_evidence_mining_prompt(section_title: str, reference_excerpt: str,
                                 folder_path: str, family: str = DATA,
                                 types: list[str] | None = None) -> str:
    """让 Draft Agent 跑一轮有界、多视角的问答,写出 evidence-pack.md。

    每个答案必须引用所依据的证据,或明确声明这个空——绝不猜测。"""
    persona_block = "\n".join(
        f"- **{name}**: {focus}" for name, focus in perspectives_for_route(family, types or [])
    )
    evidence_hint = ""
    grounding = (_LITERATURE_GROUNDING if _is_literature(types)
                 else _GROUNDING_RULE.get(family, _GROUNDING_RULE[MIXED]))
    family_note = (f"This is an evidence pack for a {family}-family section: "
                   f"the questions below are the ones this section's own evidence can "
                   f"answer. Do not import criteria from another section type.\n\n")
    boundary = _evidence_boundary(family, types)
    return (
        f"You are running PRE-DRAFT EVIDENCE MINING for section: \"{section_title}\".\n"
        f"Do not write prose for the paper yet. Your only output is an evidence pack.\n\n"
        f"{family_note}"
        f"EVIDENCE BOUNDARY: {boundary}\n\n"
        f"Adopt each of these perspectives in turn and, for each, pose at most 3 "
        f"nonredundant questions needed to assess this chapter's argument (fewer or none "
        f"when not applicable), then answer them ONLY from the "
        f"evidence described above:\n{persona_block}\n\n"
        f"Rules for every answer:\n"
        f"- {grounding}\n"
        f"- If the evidence does not support an answer, write exactly: "
        f"\"I cannot answer from the provided evidence.\" then note what is needed.\n"
        f"- Never invent numbers, citations, mechanisms, or experimental details.\n\n"
        f"Return the result as evidence-pack.md with this structure:\n"
        f"## <Perspective>\n### Q: <question>\nA: <grounded answer or the gap declaration>\n\n"
        f"Before Open Gaps, add '## Content selection': the central takeaway; main-text "
        f"essentials; material counterevidence; optional supplementary details; internal-only "
        f"provenance and checks. This is an internal evidence map, not a required paragraph "
        f"list. Do not promise unavailable supplementary material.\n\n"
        f"End with '## Open Gaps' listing every unanswered question — these mark what the "
        f"paper must NOT claim; they are not material to write about.\n\n"
        f"{evidence_hint}"
    )


def build_input_material_prompt(chapter: str, brief_path: str, folder_path: str,
                                family: str, reference_index: str,
                                reference_rule: str = "", required: list | None = None) -> str:
    """让 Manager 读取文献索引 + 本章规格 + idea,生成本章 input.md。

    input.md 装的是"本章特有、不属于全局 idea、也不属于 data 的补充素材"——主要来自
    作者提供的参考文献:针对本章该引哪些文献、各文献对本章的关键贡献点。它不是要
    Agent 复述 idea,而是把文献按本章需求组织成可用的素材边界。--init 不再生成
    input.md 空骨架,改由本步(Stage 0 的第一步)生成;已存在则由 orchestrator 跳过。
    """
    return (
        f"You are preparing the source material for chapter \"{chapter}\".\n"
        f"The document idea.md (attached) states the contribution this chapter argues for; the "
        f"references you select must serve THAT contribution, not a generic one.\n"
        f"The document brief.md (attached) is this chapter's spec (its declared `type:` and the "
        f"numbered sections it must cover).\n\n"
        f"{reference_rule}"
        + ("REQUIRED CITATIONS FOR THIS CHAPTER (author-provided; every key must appear in your table):\n"
           + "\n".join(f"{r['key']} | section {r['section']} | {r['role']}" for r in required)
           + "\n" if required else "")
        + f"From the attached ref-index.md and note:<key> documents (one row per paper; the `key` column is "
        f"the ONLY citable key), assemble the source material this chapter needs and return it as "
        f"input.md:\n"
        f"- The references this chapter needs, as a table with the header "
        f"`| key | level | section(s) | use in this chapter |`. For an A-level paper, the `use` cell "
        f"names the specific method / formula / experiment / limitation points this chapter will draw "
        f"on (use the attached note:<key> document); for a B-level paper, the one point this chapter "
        f"needs; for a C-level paper, the one-line reason it is cited. Choose by the one-line summary, "
        f"tags and level, and obey the REFERENCE RULES FOR THIS CHAPTER above.\n"
        f"- Any per-section pointers the drafter should reuse (which prior result to cite "
        f"  where, which comparison to draw).\n"
        f"- If data-index.md / the experiment settings are attached, they are the data source "
        f"  of this chapter: point to them, never state that data or a data index is missing.\n"
        f"- Keep it scoped to this chapter only; do NOT restate the contribution (it lives "
        f"  in idea.md) and do NOT list numbers (they live in data-index.md / data/).\n\n"
        f"Return only input.md. If the index is empty or nothing fits, leave a "
        f"`> [REFERENCE NEEDED: ...]` line rather than inventing a key.\n"
    )


def run_input_material(manager_agent, folder_path, chapter, family,
                       run_agent_stage, set_agent_context, verify, route: dict | None = None) -> str:
    """生成本章 input.md(本章局部素材,由 Manager 从参考文献组织)。返回 agent 结果。

    input.md 已存在则跳过(它可能含作者手填内容,绝不覆盖)。这是 Stage 0 的第一步,
    --init 不再生成 input.md 空骨架,改由本步生成;产物供后续每个起草/评审 stage 读取。
    """
    input_path = os.path.join(folder_path, "input.md")
    if os.path.exists(input_path):
        return "skipped"
    print(f"▶ 正在为【{os.path.basename(os.path.normpath(folder_path))}】挑选并整理要用的参考文献", flush=True)
    # 文献索引缺失必须报错(而不是当成空文献):备料拿到空索引,input.md 又"存在即
    # 跳过"会永久保留这份空素材。索引由 --go / --index 先行生成。
    from config import REF_INDEX_PATH, IDEA_PATH
    from .content_stage import Doc
    from .materials import all_notes_size, known_keys, keys_in_text, note_docs
    if not Path(REF_INDEX_PATH).is_file():
        raise FileNotFoundError(
            f"文献索引不存在: {REF_INDEX_PATH}(先建索引再跑章节)")
    from .chapter_contract import build_contract_reference_rule
    reference_rule = build_contract_reference_rule(route) if route else ""
    from .citation_plan import required_from_brief, ensure_input_required
    required = required_from_brief(folder_path)
    required_keys = {r["key"] for r in required}
    set_agent_context("Manager")
    brief = Doc("brief.md", os.path.join(folder_path, "brief.md"))
    head = [Doc("idea.md", IDEA_PATH), brief, Doc("ref-index.md", REF_INDEX_PATH)]
    if family in (DATA, MIXED):
        # 数据章的备料也要看到数据索引与实验设置,否则模型会在 input.md 里写
        # "没有数据索引",这句话随后被每个起草/评审阶段当成事实
        from .paths import settings_path
        if Path(data_index_path(DATA_ROOT)).is_file():
            head.append(Doc("data-index.md", data_index_path(DATA_ROOT)))
        sp = settings_path(DATA_ROOT)
        if sp.is_file():
            head.append(Doc(sp.name, sp))
    if all_notes_size() <= ALL_NOTES_BUDGET:
        notes = note_docs(sorted(known_keys()), max_notes=10_000)
    else:
        # 文献太多放不下全部笔记:先让模型只按索引挑出本章要用的,再附这些文献的笔记
        run_agent_stage(manager_agent, "Manager", (
            f"Choose the papers chapter \"{chapter}\" should cite. Read the chapter spec "
            f"(brief.md) and the reference index (ref-index.md; the `key` column is the only "
            f"citable key). Return input-keys.md: one bullet per chosen paper, `- <key>: why it "
            f"matters here`, at most 14 additional papers, most relevant first. "
            f"Required keys ({', '.join(sorted(required_keys)) or 'none'}) are already included; "
            f"do not spend these additional slots on them.\n{reference_rule}"),
            head, ["input-keys.md"])
        picked = Path(folder_path, "input-keys.md")
        keys = keys_in_text(picked.read_text(encoding="utf-8") if picked.is_file() else "")
        all_keys = sorted(required_keys | set(keys))
        notes = note_docs(all_keys, max_notes=len(all_keys))
    result = run_agent_stage(manager_agent, "Manager", build_input_material_prompt(
        chapter, "brief.md", folder_path, family, "", reference_rule=reference_rule, required=required), head + notes, ["input.md"])
    set_agent_context("Manager")
    verify(["input.md"])
    ensure_input_required(input_path, required)
    return result


def run_evidence_mining(draft_agent, folder_path, section_title,
                        reference_excerpt, run_agent_stage, verify, set_agent_context,
                        family: str = DATA, docs=None, types: list[str] | None = None):
    """执行一轮证据挖掘。可续跑:evidence-pack.md 存在即跳过。返回 agent 结果或 "skipped"。

    调用前须保证 input.md 已存在(先跑 run_input_material)。idea 全文与 data-index
    在提示词第一行点明为证据边界,Agent 自行 read_file 读取,不再依赖任何聚合文件。
    """
    if os.path.exists(os.path.join(folder_path, "evidence-pack.md")):
        print("[Manager  ] skip       | evidence-pack.md exists", flush=True)
        return "skipped"
    set_agent_context("Draft")
    result = run_agent_stage(draft_agent, "Draft", build_evidence_mining_prompt(
        section_title, reference_excerpt, folder_path, family, types=types),
        list(docs or []), ["evidence-pack.md", "todo.md"])
    set_agent_context("Manager")
    verify(["evidence-pack.md"])
    return result
