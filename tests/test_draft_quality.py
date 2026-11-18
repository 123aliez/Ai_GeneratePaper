"""初稿质量改进的离线验证(不调 API),基于纯内容阶段架构。

来自 2026-11-14 真实 API 测试(docs/test-runs/2026-11-14-senet-terra)暴露的问题:
各节普遍只写到目标字数的 10%~60%、相关工作只有三句话、实验设置章的小节类型被标题误判。

1 结构/字数检查:不到目标一半 → 交给评审作 MUST FIX;终稿结果逐节进待补清单
2 显式声明 type 的章不再按小节标题猜类型
3 收敛循环:第 2 轮起只给未通过条目 + 核验备注;第 1 轮顺带 SHOULD FIX
4 相关工作/背景章的证据挖掘视角锚定在文献笔记上
5 整篇一致性检查:跨章重复句、终稿未变则跳过、模型失败下次重试
6 定稿润色保真检查:丢引用/占位符判未核验,标题改名只提示
8 分段起草给出之前所有段的全文

运行: python tests/test_draft_quality.py
"""
import contextlib
import io
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

import agents.orchestrator as orch
import agents.outline as ol
from agents.assemble import load_chapters
from agents.chapter_type import IDEA, resolve_run_route
from agents.draft_checks import (check_structure, polish_findings, write_polish_check,
                                 read_polish_findings, ADVISORY)
from agents.evidence_mining import perspectives_for_route, build_evidence_mining_prompt
from agents.paper_review import duplicate_sentences, run_paper_review, read_paper_review
from test_pipeline_routing import make_ws_in_outline, set_idea, IDEA_TEXT, make_data, FullRunAgent

PASSED = []


def check(label, cond, detail=""):
    if not cond:
        raise AssertionError(f"{label}: {detail or 'failed'}")
    PASSED.append(label)


def run_chapter(agent, folder, outline, data_root):
    old = ol.OUTLINE_PATH
    ol.OUTLINE_PATH = Path(outline)
    buf = io.StringIO()
    try:
        orch.DATA_ROOT = data_root
        with contextlib.redirect_stdout(buf):
            results = orch.run_4stage_with_progress(agent, agent, folder, None)
    finally:
        ol.OUTLINE_PATH = old
    return results, buf.getvalue()


class _Resp:
    def __init__(self, content):
        self.content = content


class _ScriptModel:
    def __init__(self, reply):
        self.reply = reply
        self.calls = []

    def generate(self, messages, **kw):
        self.calls.append(messages)
        return _Resp(self.reply)


# ── 1. 结构/字数 ─────────────────────────────────────────────────────────
def test_structure_check_levels():
    sections = [{"title": "Overview", "target_words": 50},
                {"title": "Spectral Gate", "target_words": 20},
                {"title": "Related Methods", "target_words": 100},
                {"title": "Training Objective", "target_words": 100}]
    draft = ("## 4.1 Overview\n\n" + "word " * 50 + "\n\n### Spectral Gate\n\n" + "w " * 12
             + "\n\n### Related Methods\n\n" + "w " * 20
             + "\n\n## Limitations of This Draft\n\n" + "x " * 200)
    rows = check_structure(draft, sections)
    check("齐全的小节 OK", rows[0].startswith("OK"), str(rows))
    check("50%~70% → SHORT", rows[1].startswith("SHORT"), str(rows))
    check("不到一半 → UNDERLENGTH", rows[2].startswith("UNDERLENGTH"), str(rows))
    check("缺失的小节 MISSING", rows[3].startswith("MISSING"), str(rows))
    single = check_structure("No heading at all. " + "w " * 30,
                             [{"title": "Abstract", "target_words": 200}])
    check("短章不要求小节标题,只数字数", single[0].startswith("UNDERLENGTH"), str(single))


def test_short_draft_reaches_review_as_must_fix():
    set_idea(IDEA_TEXT)
    folder, outline, _ = make_ws_in_outline(
        "type: method\n\n1. **Framework** (~300 words)\n- 机制", "02-method")
    agent = FullRunAgent(folder)
    results, _ = run_chapter(agent, folder, outline, make_data({"test_accuracy": 0.8}))
    review = [(p, d) for p, d in zip(agent.prompts, agent.doc_sets)
              if "Return a detailed review as review-v1.md" in p]
    check("评审收到结构检查", review and "structure-check.md" in review[0][1]
          and "UNDERLENGTH section (under half its target) is a MUST FIX" in review[0][0])
    check("缺材料时允许用 [UNKNOWN: ...] 说明缺什么", "[UNKNOWN: <which material" in review[0][0])
    sc = Path(folder, "structure-check.md").read_text(encoding="utf-8")
    check("终稿结构检查覆盖初稿那份", "(final.md vs brief.md)" in sc, sc[:200])
    chapters = load_chapters([{"folder": Path(folder).name, "title": "Method",
                               "type": "method", "number": 1}], Path(folder).parent)
    check("偏短的小节进待补清单", chapters[0].structure
          and chapters[0].structure[0].startswith("UNDERLENGTH"), str(chapters[0].structure))


# ── 2. 小节类型 ──────────────────────────────────────────────────────────
def test_declared_type_is_not_overridden_by_section_titles():
    folder = Path(tempfile.mkdtemp())
    (folder / "brief.md").write_text("type: experiments\n", encoding="utf-8")
    sections = [{"number": 1, "title": "Dataset and Evaluation Protocol"},
                {"number": 2, "title": "Training and Model Comparison"}]
    route = resolve_run_route(str(folder), sections)
    check("真实测试的误判不再出现(小节不被猜成 results/method)",
          route["section_types"] == {} and route["type"] == "experiments", str(route))
    (folder / "brief.md").write_text(
        "type: experiments\n\n1. **Dataset** (~100 words)\n- type: results\n", encoding="utf-8")
    route = resolve_run_route(str(folder), sections)
    check("显式的小节级 type 仍然生效", route["section_types"].get(1) == "results", str(route))
    (folder / "brief.md").write_text("no type here\n", encoding="utf-8")
    route = resolve_run_route(str(folder), [{"number": 1, "title": "Results"}])
    check("没声明章类型时仍按标题推断", route["section_types"].get(1) == "results", str(route))


# ── 3. 收敛循环 ──────────────────────────────────────────────────────────
class OneRoundFailsAgent(FullRunAgent):
    """第 1 轮核验判 MF1 未通过(带备注),第 2 轮通过。"""

    def run_stage(self, task, docs, deliverables, out_dir, **kwargs):
        outcome = super().run_stage(task, docs, deliverables, out_dir, **kwargs)
        if "review-verify.round-1.json" in deliverables:
            Path(out_dir, "review-verify.round-1.json").write_text(json.dumps({
                "all_resolved": False,
                "items": [{"id": "MF1", "resolved": False,
                           "note": "the gating NOTE-XYZ is still unexplained"}]}),
                encoding="utf-8")
        return outcome


def test_later_rounds_see_only_open_items():
    set_idea(IDEA_TEXT)
    folder, outline, _ = make_ws_in_outline(
        "type: method\n\n1. **Framework** (~40 words)\n- 机制", "02-method")
    agent = OneRoundFailsAgent(folder)
    results, _ = run_chapter(agent, folder, outline, make_data({"test_accuracy": 0.8}))
    rounds = [p for p in agent.prompts if "Return the full revised draft as draft-v2.round-" in p]
    check("跑了两轮修订", len(rounds) == 2, str(len(rounds)))
    check("第 1 轮整张清单 + 顺带 SHOULD FIX",
          "Resolve EVERY item" in rounds[0] and "SHOULD FIX items of review-v1.md" in rounds[0])
    check("第 2 轮只给未通过项与核验备注",
          "STILL OPEN" in rounds[1] and "NOTE-XYZ" in rounds[1]
          and "Resolve EVERY item" not in rounds[1])
    verify = [p for p in agent.prompts if "VERIFY mode" in p]
    check("核验提示要求写清还缺什么", verify and all("the next revision round sees only that note" in p
                                          for p in verify))


# ── 8. 分段衔接 ──────────────────────────────────────────────────────────
def test_later_part_sees_full_earlier_parts():
    set_idea(IDEA_TEXT)
    folder, outline, _ = make_ws_in_outline(
        "type: method\n\n1. **A** (~100 words)\n- a\n2. **B** (~100 words)\n- b", "02-method")
    agent = FullRunAgent(folder)
    run_chapter(agent, folder, outline, make_data({"test_accuracy": 0.8}))
    part2 = [p for p in agent.prompts if "Write only Part 2" in p]
    check("第 2 段提示词含第 1 段全文",
          part2 and "--- Part 1 (draft-v1.part-1.md) ---" in part2[0]
          and "placeholder written by the test harness" in part2[0])


# ── 4. 文献视角 ──────────────────────────────────────────────────────────
def test_literature_perspectives():
    names = [n for n, _ in perspectives_for_route(IDEA, ["related"])]
    check("相关工作章用文献视角", names[0] == "Positioning reviewer"
          and "Thematic-grouping reviewer" in names and "Mechanism reviewer" not in names, str(names))
    bg = [n for n, _ in perspectives_for_route(IDEA, ["background"])]
    check("背景章用概念来源视角", "Concept-source reviewer" in bg, str(bg))
    prompt = build_evidence_mining_prompt("Related", "", "/ws/03-related", IDEA, types=["related"])
    check("文献章证据锚定在附上的笔记", "note:<key>" in prompt and "input.md" in prompt
          and "Mechanism reviewer" not in prompt)
    method = [n for n, _ in perspectives_for_route(IDEA, ["method"])]
    check("方法章仍用 Attribution 视角", method[0] == "Attribution reviewer")


# ── 5. 整篇一致性 ────────────────────────────────────────────────────────
def test_paper_review():
    sentence = ("The spectral gate reweights every frequency band before the encoder sees "
                "the signal, which keeps the backbone unchanged.")
    dups = duplicate_sentences({"02-method": f"Intro. {sentence}", "04-results": f"{sentence} End."})
    check("检出跨章重复长句", len(dups) == 1 and dups[0].startswith("[04-results]"), str(dups))
    ws = Path(tempfile.mkdtemp())
    meta = [{"folder": "02-method", "title": "Method", "type": "method", "number": 1},
            {"folder": "04-results", "title": "Results", "type": "results", "number": 2}]
    for m in meta:
        (ws / m["folder"]).mkdir()
        (ws / m["folder"] / "final.md").write_text(sentence, encoding="utf-8")
    model = _ScriptModel("## Findings\n- [04-results] terminology: \"gate\" vs \"filter\" — fix 04")
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        first = run_paper_review(model, meta, ws, ws / "a.md")
        second = run_paper_review(model, meta, ws, ws / "a.md")
    check("模型发现与确定性发现都进清单",
          first["status"] == "done" and len(read_paper_review(ws / "a.md")) == 2)
    check("终稿未变 → 不再调模型", second["status"] == "skipped" and len(model.calls) == 1)

    class _Quota:
        def generate(self, messages, **kw):
            raise RuntimeError("quota exhausted")
    with contextlib.redirect_stdout(buf):
        failed = run_paper_review(_Quota(), meta, ws, ws / "b.md")
        retried = run_paper_review(model, meta, ws, ws / "b.md")
    check("模型没跑成 → 下次重试,不被跳过",
          failed["status"] == "deterministic-only" and retried["status"] == "done")


# ── 6. 润色保真 ──────────────────────────────────────────────────────────
def test_polish_fidelity():
    base = ("# Method\n\nWe adopt X \\cite{a,b} with gain {{RES-001}}% [MISSING DATA].\n\n"
            "## Gate\n\nThe gate is defined in [[TAB:main]].\n")
    check("措辞改动不报", polish_findings(base, base.replace("We adopt", "We use")) == [])
    lost = polish_findings(base, base.replace(",b", "").replace(" [MISSING DATA]", "")
                           .replace("{{RES-001}}", "12.3"))
    check("丢引用/占位符/缺口标记都报",
          any("dropped citation" in x for x in lost) and any("RES-001" in x for x in lost)
          and any("MISSING DATA" in x for x in lost), str(lost))
    renamed = polish_findings(base, base.replace("## Gate", "## Gating Module"))
    check("标题改名只是提示", renamed and all(x.startswith(ADVISORY) for x in renamed), str(renamed))
    folder = Path(tempfile.mkdtemp())
    (folder / "draft-v2.md").write_text(base, encoding="utf-8")
    (folder / "final.md").write_text(base.replace(",b", ""), encoding="utf-8")
    (folder / "final.zh.md").write_text(base.replace("{{RES-001}}", ""), encoding="utf-8")
    hard = write_polish_check(str(folder))
    check("中文版丢占位符也报", any("final.zh.md" in x and "RES-001" in x for x in hard), str(hard))
    check("核验读到硬发现", len(read_polish_findings(str(folder))) == len(hard) == 3, str(hard))
    reasons = orch.final_integrity_reasons(str(folder))
    check("硬发现进核验原因", any("polish-check.md" in r for r in reasons), str(reasons))


if __name__ == "__main__":
    tests = [v for k, v in list(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in tests:
        fn()
        print(f"  ok  {fn.__name__}")
    print(f"\n{len(PASSED)} checks passed")
