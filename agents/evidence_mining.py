"""Pre-draft evidence mining (STORM's perspective-guided QA, adapted).

Before drafting a section, several review "perspectives" pose hard questions
against the active content source; each is answered from the evidence, with a
mandatory "I cannot answer from the provided evidence" guard (STORM's
AnswerQuestion fallback) so the model never fabricates to fill a gap. The
answers are collected into an evidence pack that feeds the planner and drafter,
which is what stops outline-to-prose from going hollow.

The orchestrator calls `run_evidence_mining(...)` before Stage 1; it writes
`evidence-pack.md` and is skipped on resume if that file already exists.
"""
import os
from pathlib import Path

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from config import PAPER_MODE
from .chapter_type import IDEA, DATA, MIXED

# Perspectives differ by mode AND by chapter family. Interrogating a Method
# chapter with "are the gains significant?" produces an evidence pack about
# statistics the chapter will never report; interrogating a Results chapter with
# "why is this design the right one?" produces a pack it cannot answer from
# numbers. Each family gets the questions its own evidence can actually answer.
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
# Retained name for the survey path (PAPER_MODE=survey).
SURVEY_PERSPECTIVES = [
    ("Scaling perspective", "How does this fit the parameter → compute-optimal → inference-aware line?"),
    ("Data perspective", "What are the data-engineering claims and are they evidenced?"),
    ("Architecture perspective", "Dense vs sparse/MoE vs hybrid — what is the actual design choice?"),
    ("Counter-evidence perspective", "What contradicts the main claim? What is the strongest counterexample?"),
]
# Back-compat alias: the experiment default before family routing existed.
EXPERIMENT_PERSPECTIVES = DATA_PERSPECTIVES

_FAMILY_PERSPECTIVES = {
    IDEA: IDEA_PERSPECTIVES,
    DATA: DATA_PERSPECTIVES,
    MIXED: MIXED_PERSPECTIVES,
}


def perspectives_for_mode(family: str = DATA) -> list[tuple[str, str]]:
    if PAPER_MODE != "experiment":
        return SURVEY_PERSPECTIVES
    return _FAMILY_PERSPECTIVES.get(family, MIXED_PERSPECTIVES)


# What counts as "grounding" an answer differs by family: an idea answer is
# grounded in the author's own design statement, a data answer in a logged number.
_GROUNDING_RULE = {
    IDEA: ("Ground it in a specific passage of the '## Core idea' block — quote the "
           "author's own wording for the design or claim you are relying on. A number "
           "from the results table is acceptable only as motivation, never as the "
           "explanation of why the design works."),
    DATA: ("Ground it in a specific value from the results table, quoting the metric "
           "key and the number. Never derive a value the store does not contain."),
    MIXED: ("Ground it in either a quoted passage of the '## Core idea' block or a "
            "specific value from the results table — and say which."),
}


def build_evidence_mining_prompt(section_title: str, context_pack: str,
                                 reference_excerpt: str, folder_path: str,
                                 family: str = DATA) -> str:
    """Prompt the Draft agent to run a bounded, multi-perspective QA pass and
    write evidence-pack.md. Every answer must cite the evidence it used or
    explicitly declare the gap — never guess."""
    persona_block = "\n".join(
        f"- **{name}**: {focus}" for name, focus in perspectives_for_mode(family)
    )
    evidence_hint = context_pack if PAPER_MODE == "experiment" else reference_excerpt
    grounding = (_GROUNDING_RULE.get(family, _GROUNDING_RULE[MIXED])
                 if PAPER_MODE == "experiment"
                 else "Ground it in a specific number, quote, or reference from the evidence.")
    family_note = (f"This is an evidence pack for a {family}-family section: "
                   f"the questions below are the ones this section's own evidence can "
                   f"answer. Do not import criteria from another section type.\n\n"
                   if PAPER_MODE == "experiment" else "")
    return (
        f"You are running PRE-DRAFT EVIDENCE MINING for section: \"{section_title}\".\n"
        f"Do not write prose for the paper yet. Your only output is an evidence pack.\n\n"
        f"{family_note}"
        f"Adopt each of these perspectives in turn and, for each, pose 3-5 hard, "
        f"specific questions this section must answer, then answer them ONLY from the "
        f"evidence provided below:\n{persona_block}\n\n"
        f"Rules for every answer:\n"
        f"- {grounding}\n"
        f"- If the evidence does not support an answer, write exactly: "
        f"\"I cannot answer from the provided evidence.\" then note what is needed.\n"
        f"- Never invent numbers, citations, mechanisms, or experimental details.\n\n"
        f"Write the result to '{folder_path}/evidence-pack.md' with this structure:\n"
        f"## <Perspective>\n### Q: <question>\nA: <grounded answer or the gap declaration>\n\n"
        f"End with '## Open Gaps' listing every unanswered question — these become "
        f"drafting caveats, not fabrications.\n\n"
        f"=== EVIDENCE ===\n{evidence_hint}\n"
    )


def run_evidence_mining(draft_agent, folder_path, section_title, context_pack,
                        reference_excerpt, run_agent_stage, verify, set_agent_context,
                        family: str = DATA):
    """Execute one evidence-mining pass. Resumable: skipped if evidence-pack.md
    exists. Returns the agent result, or "skipped"."""
    if os.path.exists(os.path.join(folder_path, "evidence-pack.md")):
        print("[Manager  ] skip       | evidence-pack.md exists", flush=True)
        return "skipped"
    set_agent_context("Draft")
    result = run_agent_stage(draft_agent, "Draft", build_evidence_mining_prompt(
        section_title, context_pack, reference_excerpt, folder_path, family))
    set_agent_context("Manager")
    verify(["evidence-pack.md"])
    return result
