"""引用分配、必引核验与覆盖报告的离线回归。"""

import json
import contextlib
import io
from contextlib import ExitStack, contextmanager
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import config
from agents import citation_plan as cp

PASSED = []


def check(name, cond, detail=""):
    if not cond:
        raise AssertionError(f"{name}: {detail}")
    PASSED.append(name)


def chapters():
    return [
        {"number": 1, "title": "Abstract", "type": "abstract", "sections": []},
        {"number": 2, "title": "Related Work", "type": "related",
         "sections": [{"number": 1, "title": "Fusion", "bullets": ["Compare alpha, beta."],
                       "target_words": 200, "type": ""},
                      {"number": 2, "title": "Training", "bullets": [], "target_words": 200, "type": ""}]},
        {"number": 3, "title": "Conclusion", "type": "conclusion", "sections": []},
        {"number": 4, "title": "Introduction", "type": "intro", "sections": []},
    ]


def row(key="alpha", **kwargs):
    return dict(key=key, chapter=2, section=1, role="Dynamic fusion background", status="cite",
                reason="", source="planner", **{}) | kwargs


def test_validation():
    cs, keys = chapters(), {"alpha", "beta"}
    good = [row(), row("beta", section=2)]
    check("T01 完整合法分配", not cp.validate_citation_plan(good, keys, cs))
    check("T02 缺 key", "beta" in str(cp.validate_citation_plan(good[:1], keys, cs)))
    check("T03 摘要不分配", "第 1 章" in str(cp.validate_citation_plan([row(chapter=1)], {"alpha"}, cs)))
    check("结论不分配", bool(cp.validate_citation_plan([row(chapter=3)], {"alpha"}, cs)))
    check("T04 omit 必须有理由", "reason" in str(cp.validate_citation_plan([row(status="omit")], {"alpha"}, cs)))
    check("T05 未知 key", "未知" in str(cp.validate_citation_plan([row("x")], keys, cs)))
    check("T06 小节越界", "越界" in str(cp.validate_citation_plan([row(section=8)], {"alpha"}, cs)))
    check("T07 重复位置", "重复" in str(cp.validate_citation_plan([row(), row()], {"alpha"}, cs)))
    pre = cp.prefill_from_outline(cs, keys)
    check("T08 程序预填", len(pre) == 2 and pre[0]["source"] == "outline" and pre[0]["chapter"] == 2)
    check("T20 空库空表", cp.validate_citation_plan([], set(), cs) == [])
    check("T21 无小节章填 1", not cp.validate_citation_plan([row(chapter=4)], {"alpha"}, cs))
    check("T22 同 key 多章", not cp.validate_citation_plan([row(), row(chapter=4)], {"alpha"}, cs))
    root = Path(tempfile.mkdtemp())
    p = root / "plan.md"
    p.write_text(cp.render_plan(good), encoding="utf-8")
    check("表格序列化往返", cp.parse_citation_plan(p) == good)
    p.write_text(cp.render_plan(good).replace("| 2 | 1 |", "| x | 1 |"), encoding="utf-8")
    try:
        cp.parse_citation_plan(p)
        check("整数列格式拒绝", False)
    except ValueError:
        check("整数列格式拒绝", True)
    p.write_text("no table", encoding="utf-8")
    try:
        cp.parse_citation_plan(p)
        check("无表头拒绝", False)
    except ValueError:
        check("无表头拒绝", True)


@contextmanager
def project_env():
    from agents.project import new_project
    import agents.outline as outline
    root = new_project("citation_test", Path(tempfile.mkdtemp()))
    work, inbox = root / "_work", root / "inbox"
    index = work / "index"
    index.mkdir(parents=True)
    bib = inbox / "references" / "refs.bib"
    bib.write_text("@article{alpha,title={Alpha}}\n@article{beta,title={Beta}}\n", encoding="utf-8")
    for key in ("alpha", "beta"):
        (index / "refs").mkdir(exist_ok=True)
        (index / "refs" / f"{key}.md").write_text(f"# {key} note", encoding="utf-8")
    cs = chapters()
    from agents.outline_expand import render_outline
    (work / "outline.md").write_text(render_outline(cs), encoding="utf-8")
    values = dict(HAS_PROJECT=True, PROJECT_ROOT=root, WORK_ROOT=work, INDEX_DIR=index,
                  WORKSPACE_ROOT=work / "workspace", INBOX_ROOT=inbox, REFS_BIB_PATH=bib,
                  REFERENCES_ROOT=inbox / "references", IDEA_PATH=inbox / "idea.md",
                  REF_INDEX_PATH=index / "ref-index.md", DATA_ROOT=inbox / "results",
                  OUTPUT_ROOT=root / "output")
    with ExitStack() as stack:
        for key, value in values.items():
            stack.enter_context(patch.object(config, key, value))
        stack.enter_context(patch.object(outline, "OUTLINE_PATH", work / "outline.md"))
        stack.enter_context(patch.object(outline, "WORKSPACE_ROOT", work / "workspace"))
        yield root, cs


def test_brief_and_version():
    from agents.outline import render_brief, chapter_fingerprint
    with project_env() as (root, cs):
        rows = [row(), row("beta", section=2)]
        md, cache = cp.paths()
        md.write_text(cp.render_plan(rows), encoding="utf-8")
        cp.sync_plan(cs)
        brief = render_brief(cs[1])
        check("T09 必引块只列本章", "## Required Citations" in brief and "| alpha |" in brief
              and "(none)" in render_brief(cs[3]))
        f = root / "_work" / "chapter"
        f.mkdir()
        (f / "brief.md").write_text(brief, encoding="utf-8")
        required = cp.required_from_brief(f)
        check("必引块提取小节标题", required[1]["section_title"] == "Training")
        before = [chapter_fingerprint(c) for c in cs]
        rows[0]["role"] = "Changed scientific role"
        md.write_text(cp.render_plan(rows), encoding="utf-8")
        after = [chapter_fingerprint(c) for c in cs]
        check("T10 分配修改仅影响对应章指纹", before[1] != after[1]
              and before[0] == after[0] and before[3] == after[3])
        check("正常分配未过期", not cp.check_plan_current(cs))
        config.REFS_BIB_PATH.write_text("@article{alpha,title={Changed}}", encoding="utf-8")
        check("T18 文献哈希变更硬停", "--plan-citations" in str(cp.check_plan_current(cs)))
        import run
        original = md.read_bytes()
        with contextlib.redirect_stdout(io.StringIO()):
            rc = run.plan_citations()
        check("T19 不覆盖作者分配表", rc == 1 and md.read_bytes() == original)
        md.unlink()
        config.REFS_BIB_PATH.write_text("", encoding="utf-8")
        cp.sync_plan(cs)
        check("空文献库可同步与续跑", cp.parse_citation_plan(md) == [] and not cp.check_plan_current(cs))


def test_input_notes():
    from agents.outline import render_brief
    from agents import evidence_mining as em
    from agents.materials import chapter_note_docs
    from agents.content_stage import Doc
    with project_env() as (root, cs):
        md, _ = cp.paths()
        md.write_text(cp.render_plan([row(), row("beta", section=2)]), encoding="utf-8")
        folder = root / "_work" / "chapter"
        folder.mkdir()
        (folder / "brief.md").write_text(render_brief(cs[1]), encoding="utf-8")
        config.REF_INDEX_PATH.write_text("reference index", encoding="utf-8")
        with config.REFS_BIB_PATH.open("a", encoding="utf-8") as handle:
            for key in ("optional1", "optional2", "optional3"):
                handle.write(f"@article{{{key},title={{Optional paper}}}}\n")
        received = []
        def stage(agent, role, prompt, docs, outputs):
            received.append((prompt, [d.name for d in docs]))
            if outputs == ["input-keys.md"]:
                (folder / outputs[0]).write_text("- optional1\n- optional2\n- optional3", encoding="utf-8")
            else:
                (folder / "input.md").write_text("| key | level | section(s) | use in this chapter |\n"
                                               "|---|---|---|---|\n\n## End\n", encoding="utf-8")
        with patch("agents.materials.all_notes_size", return_value=70_000), \
             patch("agents.materials.note_docs", side_effect=lambda keys, **kw: [Doc("note:" + k, text=k) for k in keys]):
            em.run_input_material(None, str(folder), "Related Work", em.IDEA, stage,
                                  lambda _: None, lambda _: True)
        text = (folder / "input.md").read_text(encoding="utf-8")
        check("T11 缺两个必引时补齐备料表", text.count("(added by citation plan)") == 2
              and text.index("beta") < text.index("## End"))
        check("T11 必引笔记进入起草", {d.name for d in chapter_note_docs(folder)} == {"note:alpha", "note:beta"})
        expected = {"note:alpha", "note:beta", "note:optional1", "note:optional2", "note:optional3"}
        check("T12 超预算必引不占挑选名额", expected.issubset(set(received[-1][1])))
        check("备料提示含必引位置与作用", "REQUIRED CITATIONS FOR THIS CHAPTER" in received[-1][0])
        old = (folder / "input.md").read_bytes()
        cp.ensure_input_required(folder / "input.md", cp.required_from_brief(folder))
        check("确定性补齐可重复执行", old == (folder / "input.md").read_bytes())


def test_required_checks():
    from agents.draft_checks import required_citation_findings
    from agents.outline import render_brief
    from agents.orchestrator import final_integrity_reasons
    from agents.prompts import GLOBAL_INSTRUCTIONS
    required = [dict(key=key, section=1, role="Background") for key in ("a", "b", "c", "d")]
    fence = chr(96) * 3
    text = "\\cite{a,b} and \\citep{c}.\n\n" + fence + "tex\n\\cite{d}\n" + fence
    findings = required_citation_findings(text, required)
    check("T13 分组和 citep 均算引用", len(findings) == 1)
    check("T14 代码块引用不算覆盖", " d " in findings[0])
    with project_env() as (root, cs):
        md, _ = cp.paths()
        md.write_text(cp.render_plan([row(), row("beta", section=2)]), encoding="utf-8")
        folder = root / "_work" / "chapter"
        folder.mkdir()
        (folder / "brief.md").write_text(render_brief(cs[1]), encoding="utf-8")
        required = cp.required_from_brief(folder)
        text = "## Fusion\n\nSee \\cite{alpha,beta}.\n\n## Training\n\nA statement."
        check("错放小节仍报缺引", "beta" in str(required_citation_findings(text, required)))
        check("按节引用通过", not required_citation_findings(
            text + " See \\cite{beta}.", required))
        opening_figure = ("[FIGURE overview: Overview]\nNote. Adapted examples \\cite{alpha}.\n\n"
                          "## Fusion\n\nOur method.\n\n## Training\n\nSee \\cite{beta}.")
        check("章首图注引用计入首节", not required_citation_findings(opening_figure, required))
        (folder / "final.md").write_text("## Fusion\n\nSee \\cite{alpha}.", encoding="utf-8")
        reasons = final_integrity_reasons(str(folder), {"type": "related"})
        check("T15 终稿缺必引判未核验", "缺少必引文献" in str(reasons) and "beta" in str(reasons))
        todo = folder / "todo.md"
        check("T15 todo 逐项列缺引", "beta" in todo.read_text(encoding="utf-8"))
        (folder / "final.md").write_text("## Fusion\n\n\\cite{alpha}\n\n## Training\n\n\\cite{beta}", encoding="utf-8")
        final_integrity_reasons(str(folder), {"type": "related"})
        check("补齐后缺引清单清除", "## Missing required citations" not in todo.read_text(encoding="utf-8"))
    check("T17 全局提示必引例外", "except the Required Citations" in GLOBAL_INSTRUCTIONS)
    source = Path("agents/orchestrator.py").read_text(encoding="utf-8")
    check("T17 评审接入缺引检查", "Treat each [MISSING-CITATION] line as a MUST FIX" in source)


def test_coverage_report():
    from agents.assemble import build_citation_coverage
    from agents.project import assemble_output, check_materials, save_state, Check
    import agents.outline as outline
    import run
    rows = [row(), row("beta", status="omit", reason="Outside the paper scope")]
    report = build_citation_coverage({"alpha", "beta"}, ["alpha"], rows,
                                     occurrences={"alpha": ["2. Related Work"]})
    check("T16 计数去重与 omit 原因", "共 2 篇；正文引用 1 篇；未引用 1 篇" in report
          and "omit 的 1 篇" in report and "Outside the paper scope" in report)
    check("T22 多章分配覆盖报告去重", "正文引用 1 篇" in build_citation_coverage(
        {"alpha"}, ["alpha"], [row(), row(chapter=4)]))
    with project_env() as (root, cs):
        md, _ = cp.paths()
        md.write_text(cp.render_plan([row(), row("beta")]), encoding="utf-8")
        from agents.outline import parse_outline
        parsed = parse_outline()
        for c in parsed:
            folder = config.WORKSPACE_ROOT / c["folder"]
            folder.mkdir(parents=True)
            body = "See \\cite{alpha}." if c["number"] == 2 else "Our study."
            (folder / "final.md").write_text(body, encoding="utf-8")
        with contextlib.redirect_stdout(io.StringIO()):
            assemble_output()
        note = (config.OUTPUT_ROOT / "待补清单.md").read_text(encoding="utf-8")
        report = (config.OUTPUT_ROOT / "引用覆盖.md").read_text(encoding="utf-8")
        check("T16 组装覆盖输出并提醒必引缺失", "引用覆盖：2 篇中 1 篇" in note and "beta" in report)
        config.IDEA_PATH.write_text("The alpha method.", encoding="utf-8")
        messages = check_materials().info
        check("材料检查点名数", any("2 篇中 1 篇在 idea.md" in msg for msg in messages))
        cp.sync_plan(parsed)
        config.REFS_BIB_PATH.write_text("@article{alpha,title={Changed}}", encoding="utf-8")
        zh = root / "outline.zh.md"
        zh.write_text("already reviewed", encoding="utf-8")
        save_state({"zh": "present"})
        captured = io.StringIO()
        with patch.object(outline, "OUTLINE_ZH_PATH", zh), \
             patch("agents.project.check_materials", return_value=Check()), \
             patch("agents.project.build_all_indexes", return_value={"references": 1, "results": 0}), \
             patch("agents.project.outline_inputs_changed", return_value=[]), \
             patch.object(run, "_translate_zh_to_en", return_value=0), \
             patch.object(run, "create_agents", side_effect=AssertionError("Must stop before model")), \
             contextlib.redirect_stdout(captured):
            rc = run.cmd_go(False)
        check("T18 go 实际入口对旧哈希硬停", rc == 1 and "--plan-citations" in captured.getvalue())


def test_planner_validation_and_many_notes():
    import run
    from agents.outline_expand import render_outline
    from agents.outline import render_brief
    from agents.materials import chapter_note_docs
    with project_env() as (root, cs):
        cs[1]["sections"][0]["bullets"] = ["Discuss fusion."]
        (config.WORK_ROOT / "outline.md").write_text(render_outline(cs), encoding="utf-8")
        def stage(agent, role, prompt, docs, outputs, dest):
            Path(dest, "citation-plan.md").write_text(cp.render_plan([row()]), encoding="utf-8")
        captured = io.StringIO()
        with patch.object(run, "_translate_zh_to_en", return_value=0), \
             patch.object(run, "_indexes_or_report", return_value={}), \
             patch.object(run, "get_manager_model", return_value=None), \
             patch("agents.create_planner_agent", return_value=None), \
             patch("agents.orchestrator.run_agent_stage_standalone", side_effect=stage), \
             contextlib.redirect_stdout(captured):
            rc = run.plan_citations()
        check("不合格规划逐项报错且不留半表", rc == 1 and "beta" in captured.getvalue()
              and not cp.paths()[0].exists() and not cp.paths()[1].exists())
        required = [row(f"paper{i}") for i in range(20)]
        cp.paths()[0].write_text(cp.render_plan(required), encoding="utf-8")
        folder = root / "_work" / "chapter"
        folder.mkdir()
        (folder / "brief.md").write_text(render_brief(cs[1]), encoding="utf-8")
        (folder / "input.md").write_text("Some source material.", encoding="utf-8")
        check("二十篇必引笔记全部送入起草", len(chapter_note_docs(folder)) == 20)
        cache = cp.paths()[1]
        cache.write_text("not json", encoding="utf-8")
        check("损坏程序版不能续跑", bool(cp.check_plan_current(cs)))


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print(f"{len(PASSED)} checks passed.")
