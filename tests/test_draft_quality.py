"""初稿质量改进的离线验证(不调 API)。

1 LaTeX 反斜杠:单轮写作原样落盘;write_file 拒收被 Python 转义吃掉的内容
2 单轮写作:文件块解析、全部必需产物齐全才落盘;整章端到端走单轮路径
3 收敛循环:第 2 轮起只给未通过条目 + 核验者备注;第 1 轮顺带 SHOULD FIX
4 相关工作/背景章的证据挖掘视角读文献笔记
5 整篇一致性检查:跨章重复句、终稿未变则跳过
6 定稿润色保真检查:丢引用/占位符判未核验,标题改名只提示
7 结构/字数检查
8 分段起草给出之前所有段的全文

运行: python tests/test_draft_quality.py
"""
import contextlib
import io
import json
import os
import re
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

import config
import agents.orchestrator as orch
import agents.outline as ol
import agents.tools as tools
from agents.prose_call import latex_escape_damage, parse_file_blocks, run_prose_call
from agents.draft_checks import (check_structure, polish_findings, write_polish_check,
                                 read_polish_findings, ADVISORY)
from agents.evidence_mining import perspectives_for_route, build_evidence_mining_prompt
from agents.chapter_type import IDEA
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


# ── 1. LaTeX 反斜杠 ──────────────────────────────────────────────────────
def test_escape_damage_detection():
    damaged = "We use $\beta$ and \frac{a}{b}, see \ref{x} and \theta."   # 故意不用 r""
    clean = r"We use $\beta$ and \frac{a}{b}, see \ref{x} and \theta \nabla \cite{k}."
    check("检出被转义吃掉的命令", latex_escape_damage(damaged) != "")
    check("原样反斜杠不误报", latex_escape_damage(clean) == "", latex_escape_damage(clean))
    check("普通缩进/换行不误报",
          latex_escape_damage("line one\n\tindented text\nnext line\r\nwindows line\n") == "")
    folder = tempfile.mkdtemp()
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        bad = tools.write_file(file_path=f"{folder}/a.md", content=damaged)
        good = tools.write_file(file_path=f"{folder}/b.md", content=clean)
    check("write_file 拒收损坏内容", bad.startswith("Error") and "RAW" in bad
          and not os.path.exists(f"{folder}/a.md"), bad)
    check("write_file 照常写干净内容", good.startswith("Successfully")
          and Path(folder, "b.md").read_text(encoding="utf-8") == clean)


# ── 2. 单轮写作 ──────────────────────────────────────────────────────────
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


def test_file_blocks_all_or_nothing():
    blocks, cut = parse_file_blocks(
        "noise\n<<<FILE: a.md>>>\n```markdown\n" + r"$\beta$ \cite{k}" + "\n```\n<<<END FILE>>>\n"
        "<<<FILE: b.md>>>\nB\n<<<END FILE>>>")
    check("切出两个文件块并去掉外层围栏",
          blocks == {"a.md": "$\\beta$ \\cite{k}\n", "b.md": "B\n"} and not cut, str(blocks))
    check("未闭合的块判为截断", parse_file_blocks("<<<FILE: a.md>>>\npartial")[1])
    folder = Path(tempfile.mkdtemp())
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        ok = run_prose_call(_ScriptModel("<<<FILE: a.md>>>\nA\n<<<END FILE>>>"), "Draft", "sys",
                            "task", [("x", "y")], {"a.md": str(folder / "a.md"),
                                                    "b.md": str(folder / "b.md")})
    check("缺一个必需产物 → 一个都不写", not ok and not any(folder.iterdir()))


class _StreamModel:
    def generate(self, messages, **kw):
        raise AssertionError("有流式接口时不该走非流式 generate")

    def generate_stream(self, messages, **kw):
        for chunk in ("<<<FILE: a.md>>>\n", r"$\beta$", "\n<<<END FILE>>>", ""):
            yield _Resp(chunk)


def test_single_turn_prefers_streaming():
    folder = Path(tempfile.mkdtemp())
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        ok = run_prose_call(_StreamModel(), "Draft", "sys", "task", [],
                            {"a.md": str(folder / "a.md")})
    check("优先走流式并拼接增量", ok and (folder / "a.md").read_text(encoding="utf-8") == "$\\beta$\n")


BODY = ("# Framework\n\nThe gate weights each band by $\\beta_k = \\frac{1}{K}$ and the "
        "encoder follows Eq.~\\ref{eq:gate}; we write $\\theta$ for its parameters.\n")


class SingleTurnModel:
    """按提示词里的 Required files 回文件块;结构化调用按任务回 JSON。"""

    def __init__(self):
        self.users = []

    def generate(self, messages, **kw):
        user = messages[-1].content
        self.users.append(user)
        if "Convert it into the machine-readable checklist" in user:
            return _Resp(json.dumps({
                "scores": {k: 4 for k in ("accuracy", "completeness", "clarity", "structure",
                                          "readability", "ai_traces", "style_consistency",
                                          "overall")},
                "decision": "REVISE",
                "must_fix": [{"id": "MF1", "location": "Framework", "issue": "gate unexplained",
                              "suggestion": "explain"}],
                "should_fix": [], "consider": [], "needs_citation": []}))
        m = re.search(r"VERIFY mode.*?(review-verify\.round-(\d+)\.json)", user, re.S)
        if m:
            resolved = m.group(2) != "1"
            return _Resp(json.dumps({"all_resolved": resolved, "items": [
                {"id": "MF1", "resolved": resolved,
                 "note": "" if resolved else "the gating NOTE-XYZ is still unexplained"}]}))
        names = re.search(r"Required files, in this order: (.+?)\. Output nothing", user).group(1).split(", ")
        out = []
        for name in names:
            if name.startswith("draft") and name != "draft-v1.plan.md" or name.startswith("final"):
                body = BODY
            else:
                body = f"# {name}\n\nNo outstanding issues.\n"
            out.append(f"<<<FILE: {name}>>>\n{body}<<<END FILE>>>")
        return _Resp("\n".join(out))


class SingleTurnAgent(FullRunAgent):
    _offline_test_stub = False   # 走单轮路径;证据挖掘/跨章交接仍走 run()

    def __init__(self, folder):
        super().__init__(folder)
        self.model = SingleTurnModel()
        self.instructions = "SYSTEM"


def test_single_turn_chapter_end_to_end():
    set_idea(IDEA_TEXT)
    folder, outline, _ = make_ws_in_outline(
        "type: method\n\n1. **Framework** (~40 words)\n- 机制", "02-method")
    data = make_data({"test_accuracy": 0.817})
    agent = SingleTurnAgent(folder)
    old = config.get_review_model
    config.get_review_model = lambda: agent.model
    try:
        results, out = run_chapter(agent, folder, outline, data)
    finally:
        config.get_review_model = old
    final = Path(folder, "final.md").read_text(encoding="utf-8")
    check("终稿里 LaTeX 反斜杠完好", "\\beta_k" in final and "\\frac{1}{K}" in final
          and "\\ref{eq:gate}" in final and latex_escape_damage(final) == "", final)
    check("整章核验通过", (results.get("verification") or {}).get("status") == "verified",
          str(results.get("verification")) + out[-2000:])
    check("起草/修订/定稿都没走 Agent",
          not any("Write only Part" in p or "frozen acceptance checklist" in p
                  or "final.md' (English)" in p for p in agent.prompts))
    users = agent.model.users
    part = [u for u in users if "Write only Part" in u]
    check("idea.md 原文拼进起草提示词", part and IDEA_TEXT.strip()[:40] in part[0])
    check("本章 brief/plan 原文拼进起草提示词",
          "BEGIN FILE: " + f"{folder}/draft-v1.plan.md" in part[0])
    rounds = [u for u in users if "Write the full revised draft" in u]
    check("跑了两轮修订", len(rounds) == 2, str(len(rounds)))
    check("第 1 轮整张清单 + 顺带 SHOULD FIX",
          "Resolve EVERY item" in rounds[0] and "SHOULD FIX items of review-v1.md" in rounds[0])
    check("第 2 轮只给未通过项与核验备注",
          "STILL OPEN" in rounds[1] and "NOTE-XYZ" in rounds[1]
          and "Resolve EVERY item" not in rounds[1])
    check("定稿与翻译拆成两次调用",
          any("Required files, in this order: final.md, decision.md, todo.md." in u for u in users)
          and any("Required files, in this order: final.zh.md." in u for u in users))
    check("润色保真检查已写且通过",
          "Status: OK" in Path(folder, "polish-check.md").read_text(encoding="utf-8"))


def test_resume_after_quota_stop_only_translates():
    """润色写完、翻译前额度用完 → 续跑只补 final.zh.md,其余阶段全部跳过。"""
    set_idea(IDEA_TEXT)
    folder, outline, _ = make_ws_in_outline(
        "type: method\n\n1. **Framework** (~40 words)\n- 机制", "02-method")
    data = make_data({"test_accuracy": 0.817})
    agent = SingleTurnAgent(folder)
    real_generate = agent.model.generate

    def quota_on_translation(messages, **kw):
        if "Required files, in this order: final.zh.md." in messages[-1].content:
            raise RuntimeError("quota exhausted")
        return real_generate(messages, **kw)
    agent.model.generate = quota_on_translation
    agent.run = lambda prompt, **kw: (
        FullRunAgent.run(agent, prompt, **kw) if "final.zh.md" not in prompt
        or "NEXT chapter" in prompt
        else (_ for _ in ()).throw(RuntimeError("quota exhausted")))
    old = config.get_review_model
    config.get_review_model = lambda: agent.model
    try:
        first, _ = run_chapter(agent, folder, outline, data)
        check("第一次停在缺中文版", Path(folder, "final.md").is_file()
              and not Path(folder, "final.zh.md").exists())
        agent.model.generate = real_generate
        agent.run = lambda prompt, **kw: FullRunAgent.run(agent, prompt, **kw)
        before = len(agent.model.users)
        second, out = run_chapter(agent, folder, outline, data)
    finally:
        config.get_review_model = old
    new_calls = agent.model.users[before:]
    check("续跑只调了翻译(Stage 5 走 Agent)",
          len(new_calls) == 1 and "final.zh.md." in new_calls[0], str(len(new_calls)))
    check("续跑后核验通过", (second.get("verification") or {}).get("status") == "verified",
          str(second.get("verification")) + out[-1500:])


# ── 3/8. Agent 路径:分段起草给全文 ──────────────────────────────────────
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
    check("文献章证据锚定在笔记上", "read_reference(key)" in prompt
          and "/ws/03-related/input.md" in prompt)
    method = [n for n, _ in perspectives_for_route(IDEA, ["method"])]
    check("方法章仍用 Attribution 视角", method[0] == "Attribution reviewer")


# ── 5. 整篇一致性 ────────────────────────────────────────────────────────
def test_paper_review_duplicates_and_skip():
    sentence = ("The spectral gate reweights every frequency band before the encoder sees "
                "the signal, which keeps the backbone unchanged.")
    dups = duplicate_sentences({"02-method": f"Intro. {sentence}", "04-results": f"{sentence} End."})
    check("检出跨章重复长句", len(dups) == 1 and dups[0].startswith("[04-results]"), str(dups))
    ws = Path(tempfile.mkdtemp())
    meta = [{"folder": "02-method", "title": "Method", "type": "method", "number": 1},
            {"folder": "04-results", "title": "Results", "type": "results", "number": 2}]
    for m, text in zip(meta, (sentence, sentence)):
        (ws / m["folder"]).mkdir()
        (ws / m["folder"] / "final.md").write_text(text, encoding="utf-8")
    out = ws / "paper-review.md"
    model = _ScriptModel("## Findings\n- [04-results] terminology: \"gate\" vs \"filter\" — fix 04")
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        first = run_paper_review(model, meta, ws, out)
        second = run_paper_review(model, meta, ws, out)
    check("模型发现与确定性发现都进清单",
          first["status"] == "done" and len(read_paper_review(out)) == 2, str(read_paper_review(out)))
    check("终稿未变 → 不再调模型", second["status"] == "skipped" and len(model.calls) == 1)

    class _Quota:
        calls = 0

        def generate(self, messages, **kw):
            _Quota.calls += 1
            raise RuntimeError("quota exhausted")
    out2 = ws / "paper-review-2.md"
    with contextlib.redirect_stdout(buf):
        failed = run_paper_review(_Quota(), meta, ws, out2)
        retried = run_paper_review(model, meta, ws, out2)
    check("模型没跑成 → 续跑会重试,不被跳过",
          failed["status"] == "deterministic-only" and retried["status"] == "done"
          and _Quota.calls == 1, str(retried["status"]))


# ── 6. 润色保真 ──────────────────────────────────────────────────────────
def test_polish_fidelity():
    base = ("# Method\n\nWe adopt X \\cite{a,b} with gain {{RES-001}}% [MISSING DATA].\n\n"
            "## Gate\n\nThe gate is defined in [[TAB:main]].\n")
    ok = polish_findings(base, base.replace("We adopt", "We use"))
    check("措辞改动不报", ok == [], str(ok))
    lost = polish_findings(base, base.replace(",b", "").replace(" [MISSING DATA]", "")
                           .replace("{{RES-001}}", "12.3"))
    check("丢引用/占位符/缺口标记都报",
          any("dropped citation" in x and "b" in x for x in lost)
          and any("RES-001" in x for x in lost) and any("MISSING DATA" in x for x in lost), str(lost))
    renamed = polish_findings(base, base.replace("## Gate", "## Gating Module"))
    check("标题改名只是提示", renamed and all(x.startswith(ADVISORY) for x in renamed), str(renamed))
    folder = Path(tempfile.mkdtemp())
    (folder / "draft-v2.md").write_text(base, encoding="utf-8")
    (folder / "final.md").write_text(base.replace(",b", ""), encoding="utf-8")
    (folder / "final.zh.md").write_text(base.replace("{{RES-001}}", ""), encoding="utf-8")
    hard = write_polish_check(str(folder))
    check("中文版丢占位符也报", any("final.zh.md" in x and "RES-001" in x for x in hard), str(hard))
    check("核验读到硬发现(英文丢 b、中文多 b、中文丢占位符)",
          len(read_polish_findings(str(folder))) == len(hard) == 3, str(hard))
    reasons = orch.final_integrity_reasons(str(folder))
    check("硬发现进核验原因", any("polish-check.md" in r for r in reasons), str(reasons))


# ── 7. 结构/字数 ─────────────────────────────────────────────────────────
def test_structure_check():
    sections = [{"title": "Overview", "target_words": 50},
                {"title": "Spectral Gate", "target_words": 20},
                {"title": "Training Objective", "target_words": 100}]
    draft = ("## 4.1 Overview\n\n" + "word " * 50 + "\n\n### Spectral Gate\n\n" + "w " * 5
             + "\n\n## Limitations of This Draft\n\n" + "x " * 200)
    rows = check_structure(draft, sections)
    check("齐全的小节 OK", rows[0].startswith("OK"), str(rows))
    check("过短的小节 SHORT", rows[1].startswith("SHORT"), str(rows))
    check("缺失的小节 MISSING", rows[2].startswith("MISSING"), str(rows))
    single = check_structure("No heading at all. " + "w " * 30, [{"title": "Abstract",
                                                                "target_words": 200}])
    check("短章不要求小节标题,只数字数", single[0].startswith("SHORT"), str(single))


if __name__ == "__main__":
    tests = [v for k, v in list(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in tests:
        fn()
        print(f"  ok  {fn.__name__}")
    print(f"\n{len(PASSED)} checks passed")
