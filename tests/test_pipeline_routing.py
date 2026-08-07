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


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for test in tests:
        test()
    print(f"\n{len(PASSED)} checks passed across {len(tests)} tests:")
    for label in PASSED:
        print(f"  ok  {label}")
    print("\nPIPELINE ROUTING TESTS PASSED")
