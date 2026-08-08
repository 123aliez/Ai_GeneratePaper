"""Pipeline-level routing test — drives run_4stage_with_progress with a fake
agent that records prompts instead of calling a model. No API, no real data.

Verifies the wiring, not just the pure functions: that the orchestrator's
pre-flight gates fire (or don't) for the right chapter types, and that what
reaches the Draft/Review prompts matches the declared type.

Run: python tests/test_pipeline_routing.py
"""
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import config
import agents.content_source as content_source
import agents.orchestrator as orch
from agents.outline import XCHAP_HEADINGS as XCHAP

PASSED = []


def check(label: str, condition: bool, detail: str = "") -> None:
    if not condition:
        raise AssertionError(f"{label}: {detail or 'failed'}")
    PASSED.append(label)


class RecordingAgent:
    """Stands in for a CodeAgent: records the prompt, writes the artifact the
    orchestrator will verify, and returns. Lets a whole run complete offline."""

    def __init__(self, folder: str):
        self.folder = folder
        self.prompts = []

    def run(self, prompt, **kwargs):
        self.prompts.append(prompt)
        # Satisfy whichever artifact this stage's verify() will look for.
        for name in ("evidence-pack.md", "draft-v1.plan.md", "todo.md",
                     "draft-v1.part-1.md", "draft-v1.part-2.md", "draft-v1.part-3.md"):
            if f"/{name}'" in prompt and not os.path.exists(os.path.join(self.folder, name)):
                Path(self.folder, name).write_text(
                    f"# {name}\n\nplaceholder written by the test harness.\n", encoding="utf-8")
        return f"wrote artifacts for prompt #{len(self.prompts)}"

    def text(self) -> str:
        return "\n\n".join(self.prompts)


def set_idea(text: str):
    """Point every module's IDEA_PATH at a temp file (or a nonexistent one)."""
    path = Path(tempfile.mkdtemp()) / "idea.md"
    if text is not None:
        path.write_text(text, encoding="utf-8")
    for module in (config, content_source, orch):
        module.IDEA_PATH = path
    return path


def make_ws(brief: str, name: str = "chapter") -> str:
    folder = Path(tempfile.mkdtemp()) / name
    folder.mkdir(parents=True)
    (folder / "brief.md").write_text(brief, encoding="utf-8")
    (folder / "input.md").write_text("source material\n", encoding="utf-8")
    return str(folder)


def make_data(metrics: dict = None) -> str:
    """Create a results store; empty dir when metrics is None."""
    root = tempfile.mkdtemp()
    if metrics:
        import json
        run = Path(root) / "run_0"
        run.mkdir()
        (run / "final_info.json").write_text(json.dumps(metrics), encoding="utf-8")
    return root


IDEA_TEXT = (
    "# Idea - Spec Module\n\n"
    "## Contribution\nWe propose the Spec module, a plug-in spectral recalibration "
    "unit that improves fine-grained classification with negligible parameter cost.\n\n"
    "## Key insight\nDiscriminative information for fine-grained categories "
    "concentrates in the mid-to-high frequency bands, so recalibrating channels in "
    "the frequency domain amplifies exactly that signal instead of averaging it away.\n\n"
    "## Design\ng(x) = sigmoid(W2 relu(W1 F(x))), applied channel-wise after an FFT, "
    "then transformed back to the spatial domain and multiplied into the feature map.\n")


def run_pipeline(folder: str, data_root: str):
    agent = RecordingAgent(folder)
    orch.DATA_ROOT = data_root
    results = orch.run_4stage_with_progress(agent, agent, folder, None)
    return agent, results


# ── 1. an idea chapter runs with an empty results store ─────────────────
def test_method_chapter_runs_without_data():
    set_idea(IDEA_TEXT)
    folder = make_ws("type: method\n\n1. **Method** (~300 words)\n- design\n")
    agent, _ = run_pipeline(folder, make_data(None))
    text = agent.text()
    check("method chapter is not blocked by an empty results store",
          len(agent.prompts) > 0, "no model call was made")
    check("the author's idea text reaches the prompt chain",
          "Spec module" in text or "Spec module" in
          Path(folder, "context-pack.md").read_text(encoding="utf-8"))
    check("idea perspectives are used for evidence mining",
          "Novelty reviewer" in text, text[:200])
    check("statistics perspectives are NOT used for a method chapter",
          "Statistics reviewer" not in text)
    check("drafter is told the idea block is primary",
          "IDEA chapter" in text and "'## Core idea' block" in text)
    pack = Path(folder, "context-pack.md").read_text(encoding="utf-8")
    check("context pack demotes the results table for a method chapter",
          "SUPPORTING EVIDENCE ONLY" in pack)


# ── 2. a results chapter with no data blocks before any model call ───────
def test_results_chapter_blocks_without_data():
    set_idea(IDEA_TEXT)
    folder = make_ws("type: results\n\n1. **Results** (~300 words)\n- numbers\n")
    agent = RecordingAgent(folder)
    orch.DATA_ROOT = make_data(None)
    results = orch.run_4stage_with_progress(agent, agent, folder, None)
    check("results chapter with no data makes no model call",
          not agent.prompts, f"{len(agent.prompts)} prompt(s) were sent")
    check("results chapter with no data returns empty results", not results, str(results))
    check("results chapter with no data produces no draft-v1",
          not os.path.exists(os.path.join(folder, "draft-v1.md")))


# ── 3. an idea chapter with no idea document blocks before any model call ─
def test_method_chapter_blocks_without_idea():
    set_idea(None)  # path exists in name only
    folder = make_ws("type: method\n\n1. **Method** (~300 words)\n- design\n")
    agent = RecordingAgent(folder)
    orch.DATA_ROOT = make_data({"accuracy": 0.817})
    results = orch.run_4stage_with_progress(agent, agent, folder, None)
    check("method chapter with no idea document makes no model call",
          not agent.prompts, f"{len(agent.prompts)} prompt(s) were sent")
    check("method chapter with no idea document returns empty results",
          not results, str(results))


def test_method_chapter_blocks_on_unfilled_skeleton():
    # The failure mode this guards: idea.md exists but is 100% template, so the
    # existence check passes and the drafter treats the template's questions as
    # the paper's claims.
    set_idea("# Idea — <方法名>\n\n"
             "> **状态:未填写**(填完请删掉这一行)\n\n"
             "## 1. 一句话贡献\n\n> 整篇论文的贡献压缩成一句。\n\n"
             "## 3. 核心洞察\n\n> 论文的啊哈点。\n\n"
             "## 6. 贡献清单\n\n1.\n2.\n3.\n")
    folder = make_ws("type: method\n\n1. **Method** (~300 words)\n- design\n")
    agent = RecordingAgent(folder)
    orch.DATA_ROOT = make_data({"accuracy": 0.817})
    results = orch.run_4stage_with_progress(agent, agent, folder, None)
    check("unfilled idea skeleton makes no model call",
          not agent.prompts, f"{len(agent.prompts)} prompt(s) were sent")
    check("unfilled idea skeleton returns empty results", not results, str(results))


def test_method_chapter_runs_on_filled_idea():
    set_idea("# Idea — Spec Module\n\n"
             "## 1. 一句话贡献\n"
             "我们提出 Spec 模块,一种谱域重标定单元,在几乎不增参数量的前提下提升细粒度分类精度。\n\n"
             "## 3. 核心洞察\n"
             "细粒度类别的判别信息集中在中高频带,在频域做通道重标定可以直接放大这部分信号。\n\n"
             "## 4. 方法设计\n"
             "输入特征图做 FFT,按通道算频带能量,过两层 MLP 得门控系数,再逆变换回空间域。\n")
    folder = make_ws("type: method\n\n1. **Method** (~300 words)\n- design\n")
    agent, _ = run_pipeline(folder, make_data(None))
    check("a filled idea document passes the pre-flight gate", len(agent.prompts) > 0)
    pack = Path(folder, "context-pack.md").read_text(encoding="utf-8")
    check("filled idea document is not flagged as a template",
          "TEMPLATE NOT FILLED IN" not in pack)
    check("filled idea text reaches the context pack", "谱域重标定" in pack)


# ── 4. a results chapter WITH data runs and is told numbers are primary ──
def test_results_chapter_runs_with_data():
    set_idea(IDEA_TEXT)
    folder = make_ws("type: results\n\n1. **Results** (~300 words)\n- numbers\n")
    agent, _ = run_pipeline(folder, make_data({"test_accuracy": 0.817}))
    text = agent.text()
    check("results chapter with data reaches the model", len(agent.prompts) > 0)
    check("data perspectives are used for evidence mining",
          "Statistics reviewer" in text, text[:200])
    check("novelty perspectives are NOT used for a results chapter",
          "Novelty reviewer" not in text)
    check("drafter is told the results are primary", "DATA chapter" in text)
    pack = Path(folder, "context-pack.md").read_text(encoding="utf-8")
    check("context pack marks results primary for a results chapter",
          "THE PRIMARY SOURCE" in pack.split("## Core idea")[0])
    check("recorded metric appears in the context pack", "0.817" in pack)


# ── 5. a related-work chapter skips the number gate entirely ─────────────
def test_related_work_skips_number_gate():
    set_idea(IDEA_TEXT)
    folder = make_ws("type: related work\n\n1. **Related Work** (~200 words)\n- prior art\n")
    agent, _ = run_pipeline(folder, make_data(None))
    check("related-work chapter runs", len(agent.prompts) > 0)
    check("related-work chapter writes no number-check.md",
          not os.path.exists(os.path.join(folder, "number-check.md")))
    check("reviewer is told the gate is off for a prose chapter",
          "number gate is OFF" in agent.text())


# ── 6. a whole-paper brief routes each draft part separately ─────────────
def test_whole_paper_parts_get_different_rules():
    set_idea(IDEA_TEXT)
    folder = make_ws(
        "type: method\n\n"
        "1. **Abstract** (~150 words)\n- headline\n\n"
        "2. **Introduction** (~250 words)\n- motivation\n\n"
        "3. **Related Work** (~200 words)\n- prior art\n\n"
        "4. **Method** (~300 words)\n- design\n\n"
        "5. **Results** (~300 words)\n- numbers\n\n"
        "6. **Conclusion** (~150 words)\n- takeaway\n")
    agent, _ = run_pipeline(folder, make_data({"test_accuracy": 0.817}))
    part_prompts = [p for p in agent.prompts if "Write only Part" in p]
    check("three part prompts were issued", len(part_prompts) == 3, str(len(part_prompts)))
    check("part 1 (abstract+intro) is routed as an idea chapter",
          "IDEA chapter" in part_prompts[0], part_prompts[0][:300])
    check("part 2 (related+method) is routed as an idea chapter",
          "IDEA chapter" in part_prompts[1])
    check("part 3 (results+conclusion) is routed as a mixed chapter",
          "MIXED chapter" in part_prompts[2], part_prompts[2][:300])
    check("part 3 names its own section types, not the chapter's",
          "results/conclusion" in part_prompts[2])


# ── 7. 两条写作路由端到端:整篇 vs 逐章 ──────────────────────────────
OUTLINE_TEXT = """\
## 1. Introduction

type: intro

### 背景 (~200 words)
- 问题设定

## 2. Method

type: method

### 总体框架 (~250 words)
- 输入输出

## 3. Results

type: results

### 主结果 (~250 words)
- 只用真实数字
"""


class FullRunAgent(RecordingAgent):
    """跑到底的 RecordingAgent:补齐 Stage 2-5 要 verify 的产物。

    RecordingAgent 只写到 draft part 就停(它服务的是前几个测试),流水线在
    Stage 2 的 verify 上返回。写作契约要验的恰恰是修订轮与定稿轮有没有带上,
    所以这里把每个阶段的产物都造出来,包括一份 schema 合法的 review-v1.json——
    orchestrator 在进收敛循环前会校验它,不合法就删掉并提前返回。
    """

    ARTIFACTS = (
        "evidence-pack.md", "draft-v1.plan.md", "todo.md",
        "draft-v1.part-1.md", "draft-v1.part-2.md", "draft-v1.part-3.md",
        "review-v1.md", "draft-v2.md", "decision.md",
        "final.md", "final.zh.md",
    )

    def run(self, prompt, **kwargs):
        import json as _json
        import re as _re
        self.prompts.append(prompt)

        for name in self.ARTIFACTS:
            if f"/{name}'" in prompt and not os.path.exists(os.path.join(self.folder, name)):
                Path(self.folder, name).write_text(
                    f"# {name}\n\nplaceholder written by the test harness.\n",
                    encoding="utf-8")

        # 收敛循环的轮次产物:名字里带轮号,按提示词里出现的文件名建。
        for name in set(_re.findall(r"(draft-v2\.round-\d+\.md)", prompt)):
            Path(self.folder, name).write_text(
                "# round draft\n\nplaceholder.\n", encoding="utf-8")

        # 结构化产物必须 schema 合法,否则 orchestrator 判为无效并提前返回。
        # 按 REVIEW_JSON_SCHEMA_HINT 的完整 schema 写,不是随手编的字段:
        # orchestrator 进收敛循环前会校验 must_fix,schema 不合法会被判无效并
        # 提前返回,那时"修订轮带了契约"这类断言根本走不到。
        if "review-v1.json" in prompt:
            Path(self.folder, "review-v1.json").write_text(_json.dumps({
                "scores": {"accuracy": 4, "completeness": 4, "clarity": 4,
                           "structure": 4, "readability": 4, "ai_traces": 4,
                           "style_consistency": 4, "overall": 4},
                "decision": "REVISE",
                "must_fix": [{"id": "MF1", "location": "section 1",
                              "issue": "placeholder", "suggestion": "placeholder"}],
                "should_fix": [], "consider": [], "needs_citation": [],
            }, ensure_ascii=False), encoding="utf-8")
        for name in set(_re.findall(r"(review-verify\.round-\d+\.json)", prompt)):
            Path(self.folder, name).write_text(_json.dumps({
                "all_resolved": True,
                "items": [{"id": "MF1", "resolved": True, "note": "placeholder"}],
            }, ensure_ascii=False), encoding="utf-8")

        # 只有真正的 Stage 5 才能改跨章状态。Stage 1a 的规划提示词里也带着这个
        # 路径(FULL 路由要读它),只按路径匹配的话 Stage 1 就会提前伪造出成功
        # 标记,真实 Stage 5 完全失效测试也照样通过——那就是自欺。
        if "so the NEXT chapter can stay consistent" in prompt:
            final_match = _re.search(r"Read '([^']+)/final\.md'", prompt)
            chapter = (Path(final_match.group(1)).name if final_match
                       else Path(self.folder).name)
            marker = f"- [{chapter}] "
            heading = "## Per-Chapter Key Claims"
            # 新契约:Agent 读跨章状态但**写候选文件**,由编排器校验后才原子替换。
            # 直接写目标文件的旧行为已经不可能通过校验——那正是这次改动要防的:
            # Agent 静默删掉前章条目时,原文件必须保持不动。
            candidate = _re.search(r"'([^']*stage5-candidate\.md)'", prompt)
            source = _re.search(r"'([^']*cross-chapter-state\.md)'", prompt)
            if candidate and source and os.path.exists(source.group(1)):
                old = Path(source.group(1)).read_text(encoding="utf-8")
                # 模拟真实的 upsert 契约:先删本章旧条目,再在 Key Claims 里写一条。
                cleaned = "\n".join(line for line in old.splitlines()
                                    if not line.startswith(marker)) + "\n"
                Path(candidate.group(1)).write_text(
                    cleaned.replace(heading,
                                    f"{heading}\n\n{marker}written by the test harness",
                                    1),
                    encoding="utf-8")

        return f"wrote artifacts for prompt #{len(self.prompts)}"


def _is_rewrite_prompt(prompt: str) -> bool:
    """这个提示词会不会产出/改写章节正文。

    写作契约必须覆盖每一个这样的阶段:规划、各起草段、修订的每一轮、定稿。漏掉
    任何一个,那一步就会按"没有契约"写,把前面按契约写好的部分改回去。
    """
    return any(marker in prompt for marker in (
        "acting only as the planner for Stage 1",
        "Write only Part",
        "Address ALL 'MUST FIX' items",
        "There are no MUST FIX items",
        "Resolve EVERY item in this frozen acceptance checklist",
        "final.zh.md",
    ))


def run_pipeline_to_end(folder: str, data_root: str):
    agent = FullRunAgent(folder)
    orch.DATA_ROOT = data_root
    return agent, orch.run_4stage_with_progress(agent, agent, folder, None)


def make_full_paper_ws(chapter_folder: str = "02-method"):
    """`--init` 生成的整篇工作区。返回 (章节目录, outline 路径, workspace 根)。"""
    import agents.outline as ol
    root = Path(tempfile.mkdtemp())
    outline = root / "outline.md"
    outline.write_text(OUTLINE_TEXT, encoding="utf-8")
    ws = root / "workspace"
    ol.init_chapter_workspaces(str(outline), str(ws))
    folder = ws / chapter_folder
    (folder / "input.md").write_text("source material\n", encoding="utf-8")
    return str(folder), str(outline), str(ws)


def with_outline(outline_path: str):
    """把 agents.outline 的默认 OUTLINE_PATH 指到临时 outline;返回还原用的原值。"""
    import agents.outline as ol
    original = ol.OUTLINE_PATH
    ol.OUTLINE_PATH = Path(outline_path)
    return ol, original


def test_full_paper_route_reaches_prompts():
    """outline 里的章:提示词必须说明它是第几章、允许跨章引用、复用前章术语。"""
    set_idea(IDEA_TEXT)
    folder, outline_path, ws = make_full_paper_ws("02-method")
    ol, original = with_outline(outline_path)
    try:
        agent, result = run_pipeline_to_end(folder, make_data(None))
    finally:
        ol.OUTLINE_PATH = original
    text = agent.text()

    check("整篇路由声明了章序号", "chapter 2 of 3" in text, text[:600])
    check("整篇路由允许跨章引用",
          "Cross-references to other chapters ARE allowed" in text)
    check("整篇路由禁止重复定义前章符号", "Do NOT re-define" in text)
    check("整篇路由要求接上文", "preceding chapter" in text and "Introduction" in text)
    check("整篇路由要求引下文", "following chapter" in text and "Results" in text)
    check("整篇路由给了邻章结构摘要", "PAPER STRUCTURE (bounded excerpt" in text)
    check("整篇路由不注入自包含约束", "SELF-CONTAINED" not in text)
    check("整篇路由让规划者读跨章状态",
          "cross-chapter-state.md" in text, text[:900])

    # Stage 5 只在 FULL 下跑:跨章状态被要求更新
    check("整篇路由跑 Stage 5 更新跨章状态",
          any("so the NEXT chapter can stay consistent" in p for p in agent.prompts),
          str([p[:60] for p in agent.prompts]))
    check("Stage 5 要求带章节前缀以便幂等重跑",
          "`- [02-method] `" in text, text[-1500:])
    xchap_text = Path(ws, "cross-chapter-state.md").read_text(encoding="utf-8")
    check("只有 Stage 5 写了 Key Claims 标记(Stage 1 没提前伪造)",
          xchap_text.count("- [02-method] ") == 1, xchap_text)
    check("跨章交接被判为成功", result.get("stage5_xchap_ok") is True, str(result))

    # 每一个会写正文的阶段都必须带 FULL 契约。漏一个,后面的轮次就会把前面按
    # 契约写好的部分改回去,而流水线全程显示成功。
    rewrite_prompts = [p for p in agent.prompts if _is_rewrite_prompt(p)]
    check("整篇测试确实经过了所有正文改写阶段",
          len(rewrite_prompts) >= 4, str([p[:70] for p in agent.prompts]))
    for prompt in rewrite_prompts:
        check("每个整篇正文改写提示词都带 FULL 契约",
              "WRITING MODE: FULL-PAPER" in prompt, prompt[:400])

    # 审稿侧同样按整篇判
    check("审稿按整篇判(查重复定义)", "re-defines a term already fixed" in text)
    check("审稿不按自包含判", "those targets do not exist" not in text)


def test_single_chapter_route_reaches_prompts():
    """手建文件夹:提示词必须要求自包含、禁止跨章过渡句、不跑 Stage 5。"""
    set_idea(IDEA_TEXT)
    folder, outline_path, ws = make_full_paper_ws("02-method")
    # 手建一个不在 outline 里的文件夹,放在同一个 workspace 下——所以
    # cross-chapter-state.md 是存在的。这正是要验的边界:文件存在也不能
    # 让 SINGLE 章读写它,判据是"这一章在不在 outline 里"。
    hand = Path(ws) / "my-chapter"
    hand.mkdir()
    (hand / "brief.md").write_text(
        "type: method\n\n1. **设计** (~250 words)\n- 机制\n", encoding="utf-8")
    (hand / "input.md").write_text("source material\n", encoding="utf-8")

    ol, original = with_outline(outline_path)
    try:
        agent, _ = run_pipeline_to_end(str(hand), make_data(None))
    finally:
        ol.OUTLINE_PATH = original
    text = agent.text()

    check("逐章路由要求自包含", "SELF-CONTAINED" in text, text[:600])
    check("逐章路由禁止跨章过渡句",
          "Do NOT write transitions that point at other chapters" in text)
    check("逐章路由不声明章序号", "chapter 2 of 3" not in text and "of 3 in" not in text)
    check("逐章路由不给邻章结构摘要", "PAPER STRUCTURE (bounded excerpt" not in text)
    check("逐章路由不允许跨章引用",
          "Cross-references to other chapters ARE allowed" not in text)
    # 跨章状态文件在同一个 workspace 下**确实存在**(整篇的章建的),这正是边界:
    # 判据是"这一章在不在 outline 里",不是"那个文件存不存在"。所以提示词里
    # 只能出现禁止读它的指令,绝不能出现它的实际路径。
    check("SINGLE 提示词明确禁止读写跨章状态",
          "Do NOT read or write any `cross-chapter-state.md`" in text, text[:900])
    check("SINGLE 提示词不给出跨章状态的实际路径",
          str(Path(ws) / "cross-chapter-state.md") not in text, text[:900])
    check("逐章路由不跑 Stage 5",
          not any("so the NEXT chapter can stay consistent" in p for p in agent.prompts),
          str([p[:60] for p in agent.prompts]))
    check("审稿按自包含判(跨章引用算 MUST FIX)",
          "those targets do not exist" in text)
    check("审稿不要求补跨章过渡", "Do NOT ask for a transition" in text)

    rewrite_prompts = [p for p in agent.prompts if _is_rewrite_prompt(p)]
    check("逐章测试确实经过了所有正文改写阶段",
          len(rewrite_prompts) >= 4, str([p[:70] for p in agent.prompts]))
    for prompt in rewrite_prompts:
        check("每个逐章正文改写提示词都带 SINGLE 契约",
              "WRITING MODE: STANDALONE CHAPTER" in prompt, prompt[:400])

    # task prompt 只是一半。Agent 的系统指令优先级更高,那里原本无条件写着
    # "读 cross-chapter-state.md 和前一章的 final.md"——只改 task prompt 的话,
    # 逐章模式下 Agent 照样会去翻它们,而这个测试看不到那个冲突。
    from agents.prompts import (DRAFT_INSTRUCTIONS, REVIEW_INSTRUCTIONS,
                                MANAGER_INSTRUCTIONS)
    for name, instructions in (("Draft", DRAFT_INSTRUCTIONS),
                               ("Review", REVIEW_INSTRUCTIONS),
                               ("Manager", MANAGER_INSTRUCTIONS)):
        check(f"{name} 系统指令把跨章上下文设为按模式条件读取",
              "STANDALONE" in instructions
              and "do not search for or read" in instructions.lower(),
              instructions[-1200:])
        check(f"{name} 系统指令不再无条件要求读跨章状态",
              "- paper/00 Background & Example/cross-chapter-state.md" not in instructions,
              instructions[:1500])


def test_mode_clause_reaches_revision_rounds():
    """修订轮也要带写作契约,否则第 2 轮会把自包含约束改回去。"""
    set_idea(IDEA_TEXT)
    folder, outline_path, ws = make_full_paper_ws("02-method")
    hand = Path(ws) / "solo-chapter"
    hand.mkdir()
    (hand / "brief.md").write_text(
        "type: method\n\n1. **设计** (~250 words)\n- 机制\n", encoding="utf-8")
    (hand / "input.md").write_text("source material\n", encoding="utf-8")

    ol, original = with_outline(outline_path)
    try:
        agent, result = run_pipeline_to_end(str(hand), make_data(None))
    finally:
        ol.OUTLINE_PATH = original

    check("SINGLE 不产生 Stage 5 成功标记", "stage5_xchap_ok" not in result,
          str(sorted(result.keys())))

    # 只取起草侧的修订提示词。审稿提示词里也有 "frozen acceptance checklist"
    # (它在告诉审稿人 must_fix 会被冻结),按那个匹配会把 Stage 2 也算进来。
    revise_prompts = [p for p in agent.prompts
                      if "Resolve EVERY item in this frozen acceptance checklist" in p
                      or "Address ALL 'MUST FIX' items" in p
                      or "There are no MUST FIX items" in p]
    check("修订阶段确实发生了", bool(revise_prompts),
          str([p[:60] for p in agent.prompts]))
    for prompt in revise_prompts:
        check("修订提示词带上自包含约束", "SELF-CONTAINED" in prompt, prompt[:400])

    final_prompts = [p for p in agent.prompts if "final.zh.md" in p]
    check("定稿阶段发生了", bool(final_prompts))
    for prompt in final_prompts:
        check("定稿提示词带上自包含约束", "SELF-CONTAINED" in prompt, prompt[:400])


# ── 8. 路由变化后必须硬停,不能报警一次后继续复用旧产物 ────────────────
def test_write_mode_change_blocks_and_keeps_artifacts():
    """手建单章后来被补进 outline:旧产物全部保留,但这一次必须停。

    写作契约从"自包含"翻成"整篇第 N 章",而已落盘的 plan / part / review / final
    全是按自包含写的。继续跑会把它们静默复用,最后 Stage 5 还把它们的术语写进整篇
    的跨章状态。这里验的是"停了"且"什么都没删"。
    """
    import agents.outline as ol

    set_idea(IDEA_TEXT)
    root = Path(tempfile.mkdtemp())
    outline = root / "outline.md"
    # 先写一份不含 02-method 的 outline,让那个目录走 SINGLE。
    outline.write_text("## 1. Introduction\n\ntype: intro\n\n"
                       "### 背景 (~100 words)\n- 问题设定\n", encoding="utf-8")
    ws = root / "workspace"
    ol.init_chapter_workspaces(str(outline), str(ws))

    hand = ws / "02-method"
    hand.mkdir()
    (hand / "brief.md").write_text(
        "type: method\n\n1. **Method** (~200 words)\n- design\n", encoding="utf-8")
    (hand / "input.md").write_text("source material\n", encoding="utf-8")

    original = ol.OUTLINE_PATH
    ol.OUTLINE_PATH = outline
    try:
        first, _ = run_pipeline_to_end(str(hand), make_data(None))
        check("首次按 SINGLE 跑出了完整产物",
              Path(hand, "final.md").exists() and bool(first.prompts))
        old_fp = orch.read_pack_fingerprint(str(hand / "context-pack.md"))
        check("旧 pack 记录的是 SINGLE 路由", "write_mode=single" in old_fp, old_fp)

        # 现在把这一章补进 outline —— 证据路由一个字没变,写作契约翻了。
        outline.write_text(OUTLINE_TEXT, encoding="utf-8")
        second, result = run_pipeline_to_end(str(hand), make_data(None))
    finally:
        ol.OUTLINE_PATH = original

    check("路由变化后不再调用任何 Agent",
          not second.prompts, str([p[:60] for p in second.prompts]))
    # 这一章现在是整篇的一章,而它的 brief 是手写的(没有 outline 指纹),
    # 所以最先命中的是 brief 来源门禁——同样是硬停,理由更具体。
    check("路由变化被门禁明确拦下", result.get("route_blocked"), str(result))
    check("昂贵的旧产物一个都没删",
          Path(hand, "final.md").exists() and Path(hand, "draft-v1.md").exists())
    check("硬停前没有覆盖旧 pack 指纹",
          orch.read_pack_fingerprint(str(hand / "context-pack.md")) == old_fp)


def test_evidence_route_change_blocks_and_keeps_artifacts():
    """改了 brief 的 `type:`(证据路由变了)同样硬停,并列出陈旧产物。

    这是 pack 指纹那条路径的直接验证:方法章改成结果章之后,evidence-pack 的
    提问视角、各 part 的路由子句、review 的判据全变了,而它们都带"存在即跳过"。
    """
    set_idea(IDEA_TEXT)
    folder = make_ws("type: method\n\n1. **设计** (~200 words)\n- 机制\n", "solo")
    data = make_data({"test_accuracy": 0.817})

    first, _ = run_pipeline_to_end(folder, data)
    check("首次按 method 跑出了完整产物",
          Path(folder, "final.md").exists() and bool(first.prompts))
    old_fp = orch.read_pack_fingerprint(os.path.join(folder, "context-pack.md"))
    check("旧 pack 记录的是 method 路由", "type=method" in old_fp, old_fp)

    Path(folder, "brief.md").write_text(
        "type: results\n\n1. **主结果** (~200 words)\n- 数字\n", encoding="utf-8")
    second, result = run_pipeline_to_end(folder, data)

    check("证据路由变化后不再调用任何 Agent",
          not second.prompts, str([p[:60] for p in second.prompts]))
    check("返回值列出了陈旧产物",
          "final.md" in result.get("stale_route_artifacts", []), str(result))
    check("陈旧清单覆盖了各阶段产物",
          {"evidence-pack.md", "draft-v1.plan.md", "review-v1.json"}
          <= set(result.get("stale_route_artifacts", [])), str(result))
    check("昂贵的旧产物一个都没删",
          Path(folder, "final.md").exists() and Path(folder, "draft-v1.md").exists())
    check("硬停前没有覆盖旧 pack 指纹",
          orch.read_pack_fingerprint(os.path.join(folder, "context-pack.md")) == old_fp)


def test_renamed_outline_folder_does_not_run_as_single():
    """改了章标题导致文件夹改名:遗留目录不能被静默当成逐章章节继续跑。"""
    set_idea(IDEA_TEXT)
    folder, outline_path, _ = make_full_paper_ws("02-method")
    # 改标题 → slug 变化 → 02-method 不再出现在 outline 里,但它带着生成指纹。
    Path(outline_path).write_text(
        OUTLINE_TEXT.replace("## 2. Method", "## 2. Renamed Method"),
        encoding="utf-8")

    ol, original = with_outline(outline_path)
    try:
        agent, result = run_pipeline_to_end(folder, make_data(None))
    finally:
        ol.OUTLINE_PATH = original

    check("改名遗留目录不调用 Agent", not agent.prompts,
          str([p[:60] for p in agent.prompts]))
    check("改名遗留目录被路由门禁明确拦下",
          result.get("route_blocked") == "generated brief is absent from current outline",
          str(result))


def test_full_chapter_with_handwritten_brief_blocks():
    """outline 里的章配一份手写 brief:没有章序号约定,按整篇跑会写错衔接。"""
    set_idea(IDEA_TEXT)
    folder, outline_path, _ = make_full_paper_ws("02-method")
    # 用手写 brief 覆盖生成的那份(去掉 outline 指纹)。
    Path(folder, "brief.md").write_text(
        "# 手写\n\ntype: method\n\n1. **设计** (~200 words)\n- 机制\n",
        encoding="utf-8")

    ol, original = with_outline(outline_path)
    try:
        agent, result = run_pipeline_to_end(folder, make_data(None))
    finally:
        ol.OUTLINE_PATH = original

    check("整篇章配手写 brief 时不调用 Agent", not agent.prompts)
    check("整篇章配手写 brief 被拦下",
          result.get("route_blocked") == "FULL chapter has a handwritten brief",
          str(result))


def test_full_chapter_without_cross_chapter_state_blocks():
    """整篇路由但跨章状态文件不存在:本章的术语约定无处落盘,必须停。"""
    set_idea(IDEA_TEXT)
    folder, outline_path, ws = make_full_paper_ws("02-method")
    Path(ws, "cross-chapter-state.md").unlink()

    ol, original = with_outline(outline_path)
    try:
        agent, result = run_pipeline_to_end(folder, make_data(None))
    finally:
        ol.OUTLINE_PATH = original

    check("缺跨章状态时不调用 Agent", not agent.prompts)
    check("缺跨章状态被拦下",
          result.get("route_blocked") == "FULL chapter has no cross-chapter state",
          str(result))


def test_cross_chapter_claim_check_is_section_scoped():
    """交接校验只认 Key Claims 小节里的标记。

    文件顶部有一份章节顺序清单,每个文件夹名本来就在里面。全文搜的话 Stage 5
    完全没跑也判成成功——这套校验就白做了。
    """
    _, _, ws = make_full_paper_ws("02-method")
    path = Path(ws, "cross-chapter-state.md")
    text = path.read_text(encoding="utf-8")

    check("顶部清单里有章名", "02-method" in text.split(XCHAP[0])[0], text[:400])
    check("只有顶部清单时判为未交接",
          not orch.cross_chapter_state_has_claim(text, "02-method"))

    filled = text.replace(XCHAP[1], f"{XCHAP[1]}\n\n- [02-method] 本章确立了 X。")
    check("Key Claims 里有标记时判为已交接",
          orch.cross_chapter_state_has_claim(filled, "02-method"))
    check("别的章仍判为未交接",
          not orch.cross_chapter_state_has_claim(filled, "03-results"))

    # 标记写错小节(落在 Terminology 里)不算交接
    misplaced = text.replace(XCHAP[0], f"{XCHAP[0]}\n\n- [02-method] 写错小节了。")
    check("标记落在别的小节不算交接",
          not orch.cross_chapter_state_has_claim(misplaced, "02-method"))
    # 标题被破坏 → 结构无效,不能判成功
    check("三个标题被破坏时判为未交接",
          not orch.cross_chapter_state_has_claim(
              filled.replace(XCHAP[2], "## 改名了"), "02-method"))


def test_stage5_cannot_delete_other_chapters():
    """Stage 5 的候选文件删掉别章条目时,必须判为未交接(原文件不被替换)。

    Review 被要求"写出完整的更新后文件",它完全可能只留下本章。前几章积累的术语
    约定是后续每一章对齐的唯一依据,被静默删掉时下游全部各写一套,而 Stage 5 自己
    显示成功——这道校验就是为此存在。
    """
    before = (
        "# Cross-chapter state\n\n"
        "章节顺序(来自 outline.md):\n"
        "- 01-intro — 1. Introduction [intro]\n"
        "- 02-method — 2. Method [method]\n\n"
        f"{XCHAP[0]}\n\n- [01-intro] SRB = Spectral Recalibration Block\n\n"
        f"{XCHAP[1]}\n\n- [01-intro] 提出问题并给出贡献清单\n\n"
        f"{XCHAP[2]}\n"
    )
    preserves = orch.cross_chapter_state_preserves_others

    upserted = before.replace(
        f"{XCHAP[1]}\n", f"{XCHAP[1]}\n\n- [02-method] 给出方法的机制与符号\n")
    check("正常 upsert 判为保留了别章",
          preserves(before, upserted, "02-method"))
    check("正常 upsert 整体判为已交接",
          orch.cross_chapter_state_has_claim(upserted, "02-method"))

    dropped = upserted.replace(
        "- [01-intro] SRB = Spectral Recalibration Block\n", "")
    check("删掉前章术语条目被判为未保留",
          not preserves(before, dropped, "02-method"))

    rewritten = upserted.replace("SRB = Spectral Recalibration Block",
                                 "SRB = 别的展开")
    check("改写前章条目内容被判为未保留",
          not preserves(before, rewritten, "02-method"))

    only_mine = (f"{XCHAP[0]}\n\n{XCHAP[1]}\n\n- [02-method] 只剩我自己\n\n"
                 f"{XCHAP[2]}\n")
    check("整个重写只留本章被判为未保留",
          not preserves(before, only_mine, "02-method"))

    # 本章自己的旧条目可以被替换掉(幂等重跑),不算破坏别章
    with_old_mine = before.replace(
        f"{XCHAP[1]}\n", f"{XCHAP[1]}\n\n- [02-method] 旧的一句话\n")
    replaced_mine = before.replace(
        f"{XCHAP[1]}\n", f"{XCHAP[1]}\n\n- [02-method] 新的一句话\n")
    check("替换本章自己的旧条目仍判为保留",
          preserves(with_old_mine, replaced_mine, "02-method"))

    # 空行变化是无害的格式差异,不该误报
    check("小节内多加空行仍判为保留",
          preserves(before, upserted.replace(f"{XCHAP[1]}\n\n",
                                             f"{XCHAP[1]}\n\n\n"), "02-method"))


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for test in tests:
        test()
    print(f"\n{len(PASSED)} checks passed across {len(tests)} tests:")
    for label in PASSED:
        print(f"  ok  {label}")
    print("\nPIPELINE ROUTING TESTS PASSED")
