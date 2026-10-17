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
from .paths import data_index_path
from .chapter_type import IDEA, DATA, MIXED

# 视角随模式与章节取材源不同。用"统计显著性如何?"去审方法章,会产出它永远不会报告
# 的统计问题;用"为什么这个设计是对的?"去审结果章,会产出它无法从数字回答的问题。
# 每个取材源拿"自己的证据真正能回答的"那组问题。
IDEA_PERSPECTIVES = [
    ("Novelty reviewer", "What exactly is new here, and what is the closest prior work it must be distinguished from?"),
    ("Mechanism reviewer", "WHY should this work? What is the causal story from design choice to expected effect?"),
    ("Precision reviewer", "Is every component, input, output, and symbol defined precisely enough to reimplement?"),
    ("Design-alternative reviewer", "Which alternative designs were possible, and why is this one chosen over them?"),
]
DATA_PERSPECTIVES = [
    ("Statistics reviewer", "Are the reported gains real? Significance, variance, seeds, confidence intervals?"),
    ("Baseline & ablation reviewer", "Are baselines fair and current? Which ablations are missing to isolate the contribution?"),
    ("Reproducibility reviewer", "Are hardware, hyperparameters, data splits, and configs sufficient to rerun this?"),
    ("Over-claim reviewer", "Which stated conclusion is stronger than the numbers support? Where is the evidence thinnest?"),
]
MIXED_PERSPECTIVES = [
    ("Claim-evidence reviewer", "For each claim about the contribution, which specific number supports it?"),
    ("Mechanism reviewer", "Do the results actually confirm the proposed mechanism, or only correlate with it?"),
    ("Over-claim reviewer", "Which stated conclusion is stronger than the numbers support?"),
    ("Limitation reviewer", "What does the evidence NOT establish, and what is the strongest counter-reading?"),
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


# "把答案锚定在证据上"对每个取材源含义不同:idea 答案锚定在作者自己的设计陈述,
# data 答案锚定在记录下来的某个数字。idea 现在是每个 stage 提示词第一行就指向的
# 全局文件,所以指令说"读 idea.md",而不是去找某个 pack 里的小节标题。
_GROUNDING_RULE = {
    IDEA: ("Ground it in a specific passage of idea.md (read it in full) — quote the "
           "author's own wording for the design or claim you are relying on. A number "
           "from data-index.md is acceptable only as motivation, never as the "
           "explanation of why the design works."),
    DATA: ("Ground it in a specific value from data-index.md (the program-generated data "
           "index), citing its `{{RES-xxx}}` id and the table/row/column. Never derive a "
           "value the index does not contain."),
    MIXED: ("Ground it in either a quoted passage of idea.md or a specific value from "
            "data-index.md — and say which."),
}


def _evidence_boundary(family: str) -> str:
    """一句话点明本节的证据边界:idea 是全局最高优先输入;data 类额外读 data-index。"""
    idea_line = (
        f"Read idea.md in full — it is the author's own statement of the contribution "
        f"(novelty, mechanism, design). It is the highest-priority input for every "
        f"section; never restate it from memory, never paraphrase the claim.")
    if family in (DATA, MIXED):
        data_line = (
            f" Also read '{data_index_path(DATA_ROOT)}' — the program-generated data index "
            f"(one grid per result table, cells shown as `{{{{RES-xxx}}}}=value`). Every number "
            f"you cite must trace to a cell there.")
        return idea_line + data_line
    return idea_line


def build_evidence_mining_prompt(section_title: str, reference_excerpt: str,
                                 folder_path: str, family: str = DATA) -> str:
    """让 Draft Agent 跑一轮有界、多视角的问答,写出 evidence-pack.md。

    每个答案必须引用所依据的证据,或明确声明这个空——绝不猜测。"""
    persona_block = "\n".join(
        f"- **{name}**: {focus}" for name, focus in perspectives_for_mode(family)
    )
    evidence_hint = ""
    grounding = _GROUNDING_RULE.get(family, _GROUNDING_RULE[MIXED])
    family_note = (f"This is an evidence pack for a {family}-family section: "
                   f"the questions below are the ones this section's own evidence can "
                   f"answer. Do not import criteria from another section type.\n\n")
    boundary = _evidence_boundary(family)
    return (
        f"You are running PRE-DRAFT EVIDENCE MINING for section: \"{section_title}\".\n"
        f"Do not write prose for the paper yet. Your only output is an evidence pack.\n\n"
        f"{family_note}"
        f"EVIDENCE BOUNDARY: {boundary}\n\n"
        f"Adopt each of these perspectives in turn and, for each, pose 3-5 hard, "
        f"specific questions this section must answer, then answer them ONLY from the "
        f"evidence described above:\n{persona_block}\n\n"
        f"Rules for every answer:\n"
        f"- {grounding}\n"
        f"- If the evidence does not support an answer, write exactly: "
        f"\"I cannot answer from the provided evidence.\" then note what is needed.\n"
        f"- Never invent numbers, citations, mechanisms, or experimental details.\n\n"
        f"Write the result to '{folder_path}/evidence-pack.md' with this structure:\n"
        f"## <Perspective>\n### Q: <question>\nA: <grounded answer or the gap declaration>\n\n"
        f"End with '## Open Gaps' listing every unanswered question — these become "
        f"drafting caveats, not fabrications.\n\n"
        f"{evidence_hint}"
    )


def build_input_material_prompt(chapter: str, brief_path: str, folder_path: str,
                                family: str, reference_index: str) -> str:
    """让 Manager 读取文献索引 + 本章规格 + idea,生成本章 input.md。

    input.md 装的是"本章特有、不属于全局 idea、也不属于 data 的补充素材"——主要来自
    作者提供的参考文献:针对本章该引哪些文献、各文献对本章的关键贡献点。它不是要
    Agent 复述 idea,而是把文献按本章需求组织成可用的素材边界。--init 不再生成
    input.md 空骨架,改由本步(Stage 0 的第一步)生成;已存在则由 orchestrator 跳过。
    """
    return (
        f"You are preparing the source material for chapter \"{chapter}\".\n"
        f"Read idea.md in full — it states the contribution this chapter argues for; the "
        f"references you select must serve THAT contribution, not a generic one.\n"
        f"Read '{brief_path}' — this chapter's spec (its declared `type:` and the "
        f"numbered sections it must cover).\n\n"
        f"From the author's reference index below (one row per paper; the `key` column is "
        f"the ONLY citable key), assemble the source material this chapter needs into a new "
        f"'{folder_path}/input.md':\n"
        f"- The references most relevant to THIS chapter's sections: for each, its `key`, "
        f"  one line on why it matters here / what claim it backs. Choose by the one-line "
        f"  summary and tags; call read_reference(key) when a summary is not enough to decide. "
        f"  Related-work style material only needs how others did it, how well, and the "
        f"  limitation — no numbers.\n"
        f"- Any per-section pointers the drafter should reuse (which prior result to cite "
        f"  where, which comparison to draw).\n"
        f"- Keep it scoped to this chapter only; do NOT restate the contribution (it lives "
        f"  in idea.md) and do NOT list numbers (they live in data-index.md / data/).\n\n"
        f"Write only '{folder_path}/input.md'. If the index is empty or nothing fits, leave a "
        f"`> [REFERENCE NEEDED: ...]` line rather than inventing a key.\n\n"
        f"=== AUTHOR REFERENCE INDEX ===\n{reference_index}\n"
    )


def run_input_material(manager_agent, folder_path, chapter, family,
                       run_agent_stage, set_agent_context, verify) -> str:
    """生成本章 input.md(本章局部素材,由 Manager 从参考文献组织)。返回 agent 结果。

    input.md 已存在则跳过(它可能含作者手填内容,绝不覆盖)。这是 Stage 0 的第一步,
    --init 不再生成 input.md 空骨架,改由本步生成;产物供后续每个起草/评审 stage 读取。
    """
    input_path = os.path.join(folder_path, "input.md")
    if os.path.exists(input_path):
        return "skipped"
    print(f"[Manager  ] Stage 0/4  | Build chapter material (from references) "
          f"→ {folder_path}/input.md", flush=True)
    # 文献索引缺失必须报错(而不是当成空文献):备料拿到空索引,input.md 又"存在即
    # 跳过"会永久保留这份空素材。索引由 --go / --index 先行生成。
    from config import REF_INDEX_PATH
    if not Path(REF_INDEX_PATH).is_file():
        raise FileNotFoundError(
            f"文献索引不存在: {REF_INDEX_PATH}(先建索引再跑章节)")
    bibliography_excerpt = Path(REF_INDEX_PATH).read_text(encoding="utf-8", errors="replace")
    set_agent_context("Manager")
    result = run_agent_stage(manager_agent, "Manager", build_input_material_prompt(
        chapter, os.path.join(folder_path, "brief.md"), folder_path, family,
        bibliography_excerpt))
    set_agent_context("Manager")
    verify(["input.md"])
    return result


def run_evidence_mining(draft_agent, folder_path, section_title,
                        reference_excerpt, run_agent_stage, verify, set_agent_context,
                        family: str = DATA):
    """执行一轮证据挖掘。可续跑:evidence-pack.md 存在即跳过。返回 agent 结果或 "skipped"。

    调用前须保证 input.md 已存在(先跑 run_input_material)。idea 全文与 data-index
    在提示词第一行点明为证据边界,Agent 自行 read_file 读取,不再依赖任何聚合文件。
    """
    if os.path.exists(os.path.join(folder_path, "evidence-pack.md")):
        print("[Manager  ] skip       | evidence-pack.md exists", flush=True)
        return "skipped"
    set_agent_context("Draft")
    result = run_agent_stage(draft_agent, "Draft", build_evidence_mining_prompt(
        section_title, reference_excerpt, folder_path, family))
    set_agent_context("Manager")
    verify(["evidence-pack.md"])
    return result
