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
from agents.content_source import idea_is_skeleton, load_idea_document
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
    """Point every module's IDEA_PATH at a temp file (or a nonexistent one).

    idea_clause (orchestrator) 也在运行时读 IDEA_PATH,所以要一并指过去。"""
    path = Path(tempfile.mkdtemp()) / "idea.md"
    if text is not None:
        path.write_text(text, encoding="utf-8")
    for module in (config, content_source, orch):
        module.IDEA_PATH = path
    return path


def make_ws_in_outline(brief: str, chapter_folder: str = "chapter") -> str:
    """单章用的工作区:从 brief 推导出 outline,再走真正的 --init 生成它。

    所有章节都必须在 outline 里,且 brief 必须带 outline 指纹(否则非生成式 brief
    门禁拦下)。这里从 brief 解析 type 与各小节(含小节级 `- type:` 与要点),镜像
    进一份单章 outline,调 init_chapter_workspaces 生成带指纹的 brief——这样路由解析、
    brief 指纹门禁、跨章状态门禁全部成立。返回 (章节目录, outline 路径, workspace 根)。
    """
    import agents.outline as ol
    import re as _re
    m = _re.search(r"type:\s*([^\n]+)", brief)
    chap_type = (m.group(1).strip() if m else "method")
    # brief 里的小节:`N. **标题** (~字数 words)` 后跟若干 `- 要点`/`- type: X` 行。
    # 把它们镜像进 outline:`### N. 标题` + 原样的 bullet 行。
    sections = _re.findall(
        r"^\s*(\d+)\.\s+\*\*(.+?)\*\*\s*\(~?(\d+)\s*words\)([^\n]*(?:\n[ \t]*-[^\n]*)*)",
        brief, _re.M)
    root = Path(tempfile.mkdtemp())
    outline = root / "outline.md"
    body = f"## 1. Chapter\n\ntype: {chap_type}\n\n"
    if sections:
        for num, title, words, tail in sections:
            body += f"### {num}. {title} (~{words} words){tail}\n\n"
    else:
        body += "### 1.1 Section (~200 words)\n- point\n\n"
    outline.write_text(body, encoding="utf-8")
    ws = root / "workspace"
    ol.init_chapter_workspaces(str(outline), str(ws))
    chapter_dirs = [p for p in ws.iterdir()
                    if p.is_dir() and not p.name.startswith("_")]
    folder = chapter_dirs[0]
    (folder / "input.md").write_text("source material\n", encoding="utf-8")
    return str(folder), str(outline), str(ws)


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


def run_pipeline_in_outline(folder: str, data_root: str, outline_path: str):
    """run_pipeline 的版本,但先把 OUTLINE_PATH 指到 outline,让该章在 outline 里。"""
    import agents.outline as ol
    original = ol.OUTLINE_PATH
    ol.OUTLINE_PATH = Path(outline_path)
    try:
        agent = RecordingAgent(folder)
        orch.DATA_ROOT = data_root
        results = orch.run_4stage_with_progress(agent, agent, folder, None)
        return agent, results
    finally:
        ol.OUTLINE_PATH = original


# ── 1. an idea chapter runs with an empty results store ─────────────────
def test_method_chapter_runs_without_data():
    set_idea(IDEA_TEXT)
    folder, outline, _ = make_ws_in_outline(
        "type: method\n\n1. **Method** (~300 words)\n- design\n", "01-method")
    agent, _ = run_pipeline_in_outline(folder, make_data(None), outline)
    text = agent.text()
    check("method chapter is not blocked by an empty results store",
          len(agent.prompts) > 0, "no model call was made")
    check("the author's idea is named in the prompt chain (idea.md pointed to)",
          "idea.md" in text, text[:800])
    check("idea perspectives are used for evidence mining",
          "Novelty reviewer" in text, text[:200])
    check("statistics perspectives are NOT used for a method chapter",
          "Statistics reviewer" not in text)
    check("drafter is told idea.md is primary (idea chapter)",
          "IDEA chapter" in text and "idea.md" in text)
    # 单章 RecordingAgent 只跑到起草段;Stage 3/4 的 idea 注入由 full-paper 测试覆盖
    # (test_full_paper_rewrite_stages_lead_with_idea)。这里只验规划/起草段开头是 idea。
    rewrite_starts = [p for p in agent.prompts
                      if any(m in p for m in (
                          "acting only as the planner for Stage 1",
                          "Write only Part"))]
    check("规划+各起草段提示词被触发", len(rewrite_starts) >= 2,
          str([p[:50] for p in agent.prompts]))
    check("规划/起草段都以 idea.md 开头",
          all(p.startswith("FIRST read idea.md") for p in rewrite_starts),
          str([p[:60] for p in rewrite_starts if not p.startswith("FIRST read idea.md")]))


# ── 2. a results chapter with no data blocks before any model call ───────
def test_results_chapter_blocks_without_data():
    set_idea(IDEA_TEXT)
    folder, outline, _ = make_ws_in_outline(
        "type: results\n\n1. **Results** (~300 words)\n- numbers\n", "01-results")
    agent, results = run_pipeline_in_outline(folder, make_data(None), outline)
    check("results chapter with no data makes no model call",
          not agent.prompts, f"{len(agent.prompts)} prompt(s) were sent")
    check("results chapter with no data returns empty results", not results, str(results))
    check("results chapter with no data produces no draft-v1",
          not os.path.exists(os.path.join(folder, "draft-v1.md")))


# ── 3. an idea chapter with no idea document blocks before any model call ─
def test_method_chapter_blocks_without_idea():
    set_idea(None)  # path exists in name only
    folder, outline, _ = make_ws_in_outline(
        "type: method\n\n1. **Method** (~300 words)\n- design\n", "01-method")
    agent, results = run_pipeline_in_outline(folder, make_data({"accuracy": 0.817}), outline)
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
    folder, outline, _ = make_ws_in_outline(
        "type: method\n\n1. **Method** (~300 words)\n- design\n", "01-method")
    agent, results = run_pipeline_in_outline(folder, make_data({"accuracy": 0.817}), outline)
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
    folder, outline, _ = make_ws_in_outline(
        "type: method\n\n1. **Method** (~300 words)\n- design\n", "01-method")
    agent, _ = run_pipeline_in_outline(folder, make_data(None), outline)
    check("a filled idea document passes the pre-flight gate", len(agent.prompts) > 0)
    # idea 现在直读全文,不再有 pack 的"模板/缺失"标注;骨架判定在 idea_is_skeleton 里。
    check("filled idea is not flagged as a skeleton",
          not idea_is_skeleton(load_idea_document())[0])
    check("filled idea is pointed to (read whole) in the prompts",
          "idea.md" in agent.text(), agent.text()[:400])


# ── 4. a results chapter WITH data runs and is told numbers are primary ──
def test_results_chapter_runs_with_data():
    set_idea(IDEA_TEXT)
    folder, outline, _ = make_ws_in_outline(
        "type: results\n\n1. **Results** (~300 words)\n- numbers\n", "01-results")
    agent, _ = run_pipeline_in_outline(folder, make_data({"test_accuracy": 0.817}), outline)
    text = agent.text()
    check("results chapter with data reaches the model", len(agent.prompts) > 0)
    check("data perspectives are used for evidence mining",
          "Statistics reviewer" in text, text[:200])
    check("novelty perspectives are NOT used for a results chapter",
          "Novelty reviewer" not in text)
    check("drafter is told the results are primary", "DATA chapter" in text)
    check("data chapter prompts point at data-index.md for numbers",
          "data-index.md" in text, text[:600])
    # 数字门禁直读 data/ 作 ground truth;data-index 只供 Agent 导航。
    # 这里验 number gate 在跑该章时确实执行过(产物存在或终端日志可见)。
    check("data chapter 跑过数字门禁(number-check.md 由门禁生成)",
          os.path.exists(os.path.join(folder, "number-check.md")),
          str([p[:50] for p in agent.prompts]))


# ── 5. a related-work chapter skips the number gate entirely ─────────────
def test_related_work_skips_number_gate():
    set_idea(IDEA_TEXT)
    folder, outline, _ = make_ws_in_outline(
        "type: related work\n\n1. **Related Work** (~200 words)\n- prior art\n",
        "01-related")
    agent, _ = run_pipeline_in_outline(folder, make_data(None), outline)
    check("related-work chapter runs", len(agent.prompts) > 0)
    check("related-work chapter writes no number-check.md",
          not os.path.exists(os.path.join(folder, "number-check.md")))
    check("reviewer is told the gate is off for a prose chapter",
          "number gate is OFF" in agent.text())


# ── 6. a whole-paper brief routes each draft part separately ─────────────
def test_whole_paper_parts_get_different_rules():
    set_idea(IDEA_TEXT)
    folder, outline, _ = make_ws_in_outline(
        "type: method\n\n"
        "1. **Abstract** (~150 words)\n- headline\n\n"
        "2. **Introduction** (~250 words)\n- motivation\n\n"
        "3. **Related Work** (~200 words)\n- prior art\n\n"
        "4. **Method** (~300 words)\n- design\n\n"
        "5. **Results** (~300 words)\n- numbers\n\n"
        "6. **Conclusion** (~150 words)\n- takeaway\n", "01-whole")
    agent, _ = run_pipeline_in_outline(folder, make_data({"test_accuracy": 0.817}), outline)
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


# ── 7. 整篇路由端到端:写作契约到达每个阶段 ──────────────────────────
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
        # 路径(规划者要读它),只按路径匹配的话 Stage 1 就会提前伪造出成功标记,
        # 真实 Stage 5 完全失效测试也照样通过——那就是自欺。
        if "so the NEXT chapter can stay consistent" in prompt:
            final_match = _re.search(r"Read '([^']+)/final\.md'", prompt)
            chapter = (Path(final_match.group(1)).name if final_match
                       else Path(self.folder).name)
            marker = f"- [{chapter}] "
            heading = "## Per-Chapter Key Claims"
            # 新契约:Agent 读跨章状态但**写候选文件**,由编排器校验后才原子替换。
            # 直接写目标文件的旧行为已经不可能通过校验——那正是这次改动要防的:
            # Agent 静默删掉前章条目时,原文件必须保持不动。
            candidate = _re.search(r"'([^']*cross-chapter-draft\.md)'", prompt)
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


def run_pipeline_to_end(folder: str, data_root: str, outline_path: str = None):
    """跑完整流水线。若给定 outline_path,把 OUTLINE_PATH 指过去(让该章在 outline 里)。"""
    import agents.outline as ol
    original = ol.OUTLINE_PATH
    if outline_path is not None:
        ol.OUTLINE_PATH = Path(outline_path)
    try:
        agent = FullRunAgent(folder)
        orch.DATA_ROOT = data_root
        return agent, orch.run_4stage_with_progress(agent, agent, folder, None)
    finally:
        ol.OUTLINE_PATH = original


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


def test_full_paper_route_reaches_prompts():
    """outline 里的章:提示词必须说明它是第几章、允许跨章引用、复用前章术语。"""
    set_idea(IDEA_TEXT)
    folder, outline_path, ws = make_full_paper_ws("02-method")
    agent, result = run_pipeline_to_end(folder, make_data(None), outline_path)
    text = agent.text()

    check("整篇路由声明了章序号", "chapter 2 of 3" in text, text[:600])
    check("整篇路由允许跨章引用",
          "Cross-references to other chapters ARE allowed" in text)
    check("整篇路由禁止重复定义前章符号", "Do NOT re-define" in text)
    check("整篇路由要求接上文", "preceding chapter" in text and "Introduction" in text)
    check("整篇路由要求引下文", "following chapter" in text and "Results" in text)
    check("整篇路由给了邻章结构摘要", "PAPER STRUCTURE (bounded excerpt" in text)
    check("整篇路由让规划者读跨章状态",
          "cross-chapter-state.md" in text, text[:900])

    # 每个正常完成的章节都必须跑 Stage 5。
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
        check("每个整篇正文改写阶段都以 idea.md 开头",
              prompt.startswith("FIRST read idea.md"),
              prompt[:80])

    # 审稿侧同样按整篇判
    check("审稿按整篇判(查重复定义)", "re-defines a term already fixed" in text)


def test_full_paper_rewrite_stages_lead_with_idea():
    """整篇跑到底:Stage 1a / 1b~1c / Stage 3 每一轮 / Stage 4 都必须以 idea.md 开头。

    单章 RecordingAgent 只跑到起草段,验不到修订/定稿。这里用 FullRunAgent 跑到底,
    确认每一个会改写正文的阶段(含收敛循环的三种分支与定稿)都先读 idea.md——
    漏一个,那一步就会不读 idea 直接改,把前面按 idea 写好的部分改回去。
    """
    set_idea(IDEA_TEXT)
    folder, outline_path, ws = make_full_paper_ws("02-method")
    agent, _ = run_pipeline_to_end(folder, make_data({"test_accuracy": 0.817}), outline_path)
    rewrite_prompts = [p for p in agent.prompts if _is_rewrite_prompt(p)]
    check("整篇跑到了足够多的改写阶段", len(rewrite_prompts) >= 4,
          str([p[:50] for p in rewrite_prompts]))
    # 覆盖 Stage 1a(规划)、起草段、修订轮、定稿四类标记各至少一次。
    kinds = {
        "Stage 1a 规划": any("acting only as the planner for Stage 1" in p for p in rewrite_prompts),
        "起草段": any("Write only Part" in p for p in rewrite_prompts),
        "修订轮": any(("Address ALL 'MUST FIX' items" in p
                       or "Resolve EVERY item in this frozen acceptance checklist" in p
                       or "There are no MUST FIX items" in p) for p in rewrite_prompts),
        "定稿": any("final.zh.md" in p for p in rewrite_prompts),
    }
    for name, hit in kinds.items():
        check(f"整篇跑到了 {name} 阶段", hit)
    check("每个正文改写阶段都以 idea.md 开头",
          all(p.startswith("FIRST read idea.md") for p in rewrite_prompts),
          str([p[:60] for p in rewrite_prompts if not p.startswith("FIRST read idea.md")]))


def test_system_prompts_require_cross_chapter_context():
    """三类 Agent 的系统指令都必须读取任务显式传入的跨章上下文。"""
    from agents.prompts import (
        DRAFT_INSTRUCTIONS, MANAGER_INSTRUCTIONS, REVIEW_INSTRUCTIONS,
    )
    for name, instructions in (("Draft", DRAFT_INSTRUCTIONS),
                               ("Review", REVIEW_INSTRUCTIONS)):
        check(f"{name} 系统指令要求读取跨章上下文",
              "Cross-chapter context: read only the cross-chapter-state / structure paths"
              in instructions, instructions[-1400:])
    check("Manager 系统指令要求读取跨章上下文",
          "Cross-chapter context: read the cross-chapter-state.md path"
          in MANAGER_INSTRUCTIONS, MANAGER_INSTRUCTIONS[-1400:])


def test_chapter_outside_outline_blocks():
    """不在 outline 里的文件夹不再是合法章节:流水线在路由解析处硬停,不调模型。"""
    set_idea(IDEA_TEXT)
    folder, outline_path, ws = make_full_paper_ws("02-method")
    # 手建一个不在 outline 里的文件夹(同一个 workspace,cross-chapter-state.md 存在)。
    hand = Path(ws) / "my-chapter"
    hand.mkdir()
    (hand / "brief.md").write_text(
        "type: method\n\n1. **设计** (~250 words)\n- 机制\n", encoding="utf-8")
    (hand / "input.md").write_text("source material\n", encoding="utf-8")

    agent, result = run_pipeline_to_end(str(hand), make_data(None), outline_path)

    check("不在 outline 的章节不调用任何 Agent",
          not agent.prompts, str([p[:60] for p in agent.prompts]))
    check("路由解析失败被记录", result.get("route_blocked"), str(result))


def test_run_all_rejects_workspace_outline_drift():
    """--all 不能跳过多余目录或遗漏尚未生成的 outline 章节(否则会报告"全部完成")。"""
    import run as runner
    import agents.outline as ol

    root = Path(tempfile.mkdtemp())
    outline = root / "outline.md"
    outline.write_text(
        "## 1. Introduction\n\ntype: intro\n\n"
        "### 背景 (~100 words)\n- 问题\n\n"
        "## 2. Method\n\ntype: method\n\n"
        "### 方法 (~100 words)\n- 机制\n", encoding="utf-8")
    ws = root / "workspace"
    ol.init_chapter_workspaces(str(outline), str(ws))
    expected = [chapter["folder"] for chapter in ol.parse_outline(outline)]

    old_root, old_outline = runner.PAPER_ROOT, ol.OUTLINE_PATH
    runner.PAPER_ROOT = str(ws)
    ol.OUTLINE_PATH = outline
    try:
        folders, extra = runner.outline_chapters_in_order()
        check("工作区与 outline 一致时返回完整顺序",
              folders == expected and not extra, str((folders, extra)))

        # 多出一个磁盘残留目录 → 拒绝,不静默跳过。
        legacy = ws / "legacy-chapter"
        legacy.mkdir()
        (legacy / "brief.md").write_text("type: method\n", encoding="utf-8")
        try:
            runner.outline_chapters_in_order()
        except ol.OutlineRouteError as exc:
            check("--all 拒绝不在 outline 的目录",
                  "legacy-chapter" in str(exc), str(exc))
        else:
            raise AssertionError("--all 静默跳过了不在 outline 的目录")

        (legacy / "brief.md").unlink()
        legacy.rmdir()
        # 缺一个 outline 里的目录(删掉它的 brief) → 拒绝,不静默漏跑。
        (ws / expected[-1] / "brief.md").unlink()
        try:
            runner.outline_chapters_in_order()
        except ol.OutlineRouteError as exc:
            check("--all 拒绝遗漏 outline 章节",
                  expected[-1] in str(exc), str(exc))
        else:
            raise AssertionError("--all 静默遗漏了尚未生成的 outline 章节")
    finally:
        runner.PAPER_ROOT = old_root
        ol.OUTLINE_PATH = old_outline


def test_evidence_route_change_blocks_and_keeps_artifacts():
    """改了 brief 的路由(加 `- type: results`)同样硬停,并列出陈旧产物。

    context-pack 删除后,路由变化改由 brief.md 首行的 outline 指纹(chapter_fingerprint,
    覆盖 type + 小节级 type + 要点)统一捕获:方法章的小节被改成 results 小节后,指纹
    不符即硬停,并把 evidence-pack / plan / review 等带"存在即跳过"的旧产物一并列出。
    """
    set_idea(IDEA_TEXT)
    folder, outline, ws = make_ws_in_outline(
        "type: method\n\n1. **Framework** (~200 words)\n- 机制")
    data = make_data({"test_accuracy": 0.817})

    first, _ = run_pipeline_to_end(folder, data, outline)
    check("首次按 method 跑出了完整产物",
          Path(folder, "final.md").exists() and bool(first.prompts))
    old_fp = orch.read_brief_fingerprint(os.path.join(folder, "brief.md"))
    check("旧 brief 指纹记录的是 method 路由", "type=method" in old_fp, old_fp)

    # 给唯一小节加 `- type: results`(章标题/type 不变 → 文件夹名稳定)。这让 brief
    # 指纹的 section 段变化(无 type → 有 results)。**不重新 --init**:模拟"改了 outline
    # 的 type 却直接重跑"的真实疏漏——brief.md 还是旧指纹,与改后的 outline 不符即硬停。
    outline_content = Path(outline).read_text(encoding="utf-8")
    Path(outline).write_text(outline_content.rstrip() + "\n- type: results\n",
                             encoding="utf-8")
    import agents.outline as ol
    new_fp = ol.chapter_fingerprint(
        ol.resolve_write_mode(Path(folder).name, outline_path=outline)["chapter"])
    second, result = run_pipeline_to_end(folder, data, outline)

    check("路由变化后不再调用任何 Agent",
          not second.prompts, str([p[:60] for p in second.prompts]))
    check("返回值列出了陈旧产物",
          "final.md" in result.get("stale_route_artifacts", []), str(result))
    check("陈旧清单覆盖了各阶段产物",
          {"evidence-pack.md", "draft-v1.plan.md", "review-v1.json"}
          <= set(result.get("stale_route_artifacts", [])), str(result))
    check("昂贵的旧产物一个都没删",
          Path(folder, "final.md").exists() and Path(folder, "draft-v1.md").exists())
    check("brief.md 仍是旧路由指纹(硬停在 --init 之前,不自动刷新)",
          orch.read_brief_fingerprint(os.path.join(folder, "brief.md")) == old_fp)
    check("新路由指纹确实 differs 于旧的", new_fp != old_fp)


def test_renamed_outline_folder_is_not_a_chapter():
    """改了章标题导致文件夹改名:遗留目录不在 outline 里,路由解析直接报错。"""
    set_idea(IDEA_TEXT)
    folder, outline_path, _ = make_full_paper_ws("02-method")
    # 改标题 → slug 变化 → 02-method 不再出现在 outline 里,但它带着生成指纹。
    Path(outline_path).write_text(
        OUTLINE_TEXT.replace("## 2. Method", "## 2. Renamed Method"),
        encoding="utf-8")

    agent, result = run_pipeline_to_end(folder, make_data(None), outline_path)

    check("改名遗留目录不调用 Agent", not agent.prompts,
          str([p[:60] for p in agent.prompts]))
    check("改名遗留目录被路由解析拦下(不在 outline)", result.get("route_blocked"),
          str(result))


def test_full_chapter_with_handwritten_brief_blocks():
    """outline 里的章配一份手写 brief:没有章序号约定,按整篇跑会写错衔接。"""
    set_idea(IDEA_TEXT)
    folder, outline_path, _ = make_full_paper_ws("02-method")
    # 用手写 brief 覆盖生成的那份(去掉 outline 指纹)。
    Path(folder, "brief.md").write_text(
        "# 手写\n\ntype: method\n\n1. **设计** (~200 words)\n- 机制\n",
        encoding="utf-8")

    agent, result = run_pipeline_to_end(folder, make_data(None), outline_path)

    check("整篇章配手写 brief 时不调用 Agent", not agent.prompts)
    check("整篇章配手写 brief 被拦下",
          result.get("route_blocked") == "chapter has a non-generated brief",
          str(result))


def test_full_chapter_without_cross_chapter_state_blocks():
    """跨章状态文件不存在:本章的术语约定无处落盘,必须停。"""
    set_idea(IDEA_TEXT)
    folder, outline_path, ws = make_full_paper_ws("02-method")
    Path(ws, "cross-chapter-state.md").unlink()

    agent, result = run_pipeline_to_end(folder, make_data(None), outline_path)

    check("缺跨章状态时不调用 Agent", not agent.prompts)
    check("缺跨章状态被拦下",
          result.get("route_blocked") == "chapter has no cross-chapter state",
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
