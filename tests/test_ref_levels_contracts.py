"""文献分级与章节契约(不调模型)。

运行: python tests/test_ref_levels_contracts.py

T01–T10 文献室扫描与索引;T11–T18 契约子句、流水线注入、粗检、组装与指纹;
T20 新建项目的级别文件夹;T21 评审标准不再评判创新性。
"""
import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

import config
from agents.chapter_contract import (CONTRACTS, DEFAULT_CONTRACT, NOVELTY_GUARD,
                                     build_contract_clause, build_contract_review_clause,
                                     check_contract, contract_for, write_contract_check)
from agents.chapter_type import CHAPTER_TYPES
from agents.prompts import REVIEW_INSTRUCTIONS
from agents.project import new_project
from agents.references import build_reference_index, scan_library
from agents.skills_registry import STAGE_PROMPT_VERSION, skills_fingerprint
from agents.tools import search_reference_text
from test_pipeline_routing import (FullRunAgent, IDEA_TEXT, make_data, make_full_paper_ws,
                                   make_ws_in_outline, set_idea)

PASSED = []


def check(label, cond, detail=""):
    if not cond:
        raise AssertionError(f"{label}: {detail or 'failed'}")
    PASSED.append(label)


def w(path, text):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(text, encoding="utf-8")


def note(title, doi="", arxiv="", summary="一句话。", extra=""):
    head = ["---", f'title: "{title}"']
    if doi:
        head.append(f'doi: "{doi}"')
    if arxiv:
        head.append(f'arxiv: "{arxiv}"')
    head += ["---", "", "## 一句话总结", summary, ""]
    return "\n".join(head) + extra


BIB = "\n".join([
    "@article{qmf, title={Query Matching Fusion}, year={2024}, doi={10.1000/qmf}}",
    "@article{yoon, title={Yoon Paper}, year={2026}, eprint={2601.00001}}",
    "@article{fungi, title={Fungal Classification}, year={2023}}",
    "@article{convnext, title={A ConvNet for the 2020s}, year={2022}, doi={10.1000/convnext}}",
    "@article{daft, title={Dynamic Affine Feature Map Transform}, year={2021}}",
    "@article{orphan, title={No Note At All}, year={2020}}",
    "",
])


def fixture() -> Path:
    """方案 7.2 的固定夹具:六条 bib、五份笔记、两份放错位置的笔记、若干干扰项。"""
    root = Path(tempfile.mkdtemp())
    w(root / "refs.bib", BIB)
    w(root / "A" / "qmf.md", note("Query Matching Fusion", doi="10.1000/qmf",
                                  summary="QMF 融合。", extra="## 整体框架\n\nQMF 的框架正文。\n"))
    w(root / "A" / "yoon" / "笔记.md", note("Yoon Paper", arxiv="2601.00001", summary="Yoon 的方法。"))
    w(root / "A" / "yoon" / "paper.md", "# Yoon\n\nThe yoon-unique-term appears here.\n")
    w(root / "B" / "fungi" / "笔记.md", note("Fungal Classification", summary="真菌分类。"))
    w(root / "B" / "fungi" / "paper.md", "# Fungi\n\nFungi fulltext body.\n")
    w(root / "C" / "convnext.md", note("A ConvNet for the 2020s", doi="10.1000/convnext",
                                       summary="ConvNeXt 回归卷积。"))
    w(root / "C" / "daft" / "笔记.md", note("Dynamic Affine Feature Map Transform", summary="DAFT 变换。",
                                           extra="## 整体框架\n\nUNIQUE-C-BODY-LINE\n"))
    w(root / "C" / "daft" / "paper.md", "# DAFT\n\nDAFT fulltext body about the framework.\n")
    # 放错位置:不进索引,也不触发"都对应"。
    w(root / "misc" / "x" / "笔记.md", note("Dynamic Affine Feature Map Transform", summary="错位笔记。"))
    w(root / "a" / "z.md", note("Something Else", doi="10.1000/else", summary="小写文件夹。"))
    # 级别文件夹里的问题文件与应被忽略的文件夹。
    w(root / "A" / "loose.md", "# 普通 md\n\n不是笔记。\n")
    w(root / "B" / "bad" / "x.md", "# plain\n\n没有 YAML 头。\n")
    w(root / "A" / "assets" / "img.md", "图片说明\n")
    w(root / "B" / "_draft" / "n.md", note("Draft Note", summary="草稿。"))
    return root


def test_scan_and_index():
    root = fixture()
    lib = scan_library(root)
    keys = set(lib.records)
    check("T01 records 恰为五篇", keys == {"qmf", "yoon", "fungi", "convnext", "daft"}, str(keys))
    check("T01 级别依次 A A B C C",
          [lib.records[k].level for k in ("qmf", "yoon", "fungi", "convnext", "daft")] == ["A", "A", "B", "C", "C"])
    check("T01 folder 相对路径",
          [lib.records[k].folder for k in ("qmf", "yoon", "fungi", "convnext", "daft")]
          == ["A/qmf.md", "A/yoon", "B/fungi", "C/convnext.md", "C/daft"],
          str([lib.records[k].folder for k in keys]))

    check("T02 yoon/fungi/daft 有原文",
          all(lib.records[k].fulltext_path for k in ("yoon", "fungi", "daft")))
    check("T02 直接放的笔记没有原文",
          lib.records["qmf"].fulltext_path is None and lib.records["convnext"].fulltext_path is None)
    check("T02 直接放的笔记不发没有原文警告",
          not any("qmf.md" in x and "没有原文" in x for x in lib.warnings)
          and not any("convnext.md" in x and "没有原文" in x for x in lib.warnings), str(lib.warnings))

    check("T03 非级别文件夹两条警告",
          any("misc/ 不在 A/B/C" in x for x in lib.warnings)
          and any("a/ 不在 A/B/C" in x for x in lib.warnings), str(lib.warnings))
    check("T03 错位笔记没进 records 且没有都对应",
          "else" not in "".join(lib.records) and not any("都对应" in e for e in lib.errors), str(lib.errors))

    check("T04 非笔记 md 只警告", any("A/loose.md 不是模板笔记" in x for x in lib.warnings), str(lib.warnings))
    check("T04 loose 不进 errors", not any("loose" in e for e in lib.errors), str(lib.errors))

    check("T05 无笔记的子文件夹仍是错误", any("B/bad/" in e and "没有找到模板笔记" in e for e in lib.errors),
          str(lib.errors))
    check("T05 assets 与 _draft 不产生提示",
          not any("assets" in x or "_draft" in x for x in lib.errors + lib.warnings),
          str(lib.errors + lib.warnings))

    w(root / "B" / "qmf2" / "n.md", note("Another", doi="10.1000/qmf", summary="重复 doi。"))
    lib2 = scan_library(root)
    check("T06 跨级别重复报都对应", any("都对应" in e and "qmf" in e for e in lib2.errors), str(lib2.errors))

    lib = scan_library(fixture())
    out = Path(tempfile.mkdtemp())
    build_reference_index(lib, out)
    idx = (out / "ref-index.md").read_text(encoding="utf-8")
    check("T07 表头", "| key | 级别 | 标题 | 年份 | 出处 | tags | 一句话总结 | 笔记 | 原文 |" in idx)
    rows = [ln for ln in idx.splitlines() if ln.startswith("| ") and not ln.startswith("| key") and not ln.startswith("|---")]
    order = [ln.split("|")[1].strip() for ln in rows]
    check("T07 行序 A→B→C 再无笔记", order == ["qmf", "yoon", "fungi", "convnext", "daft", "orphan"], str(order))
    orphan = next(ln for ln in rows if ln.split("|")[1].strip() == "orphan")
    check("T07 orphan 级别为 — 且笔记为无",
          orphan.split("|")[2].strip() == "—" and orphan.split("|")[8].strip() == "无", orphan)
    daft = next(ln for ln in rows if ln.split("|")[1].strip() == "daft")
    check("T07 daft 原文列为 —", daft.split("|")[9].strip() == "—", daft)
    check("T07 说明行含 C 级规则", "C 级:只有一句话总结" in idx)

    qmf_view = (out / "refs" / "qmf.md").read_text(encoding="utf-8")
    daft_view = (out / "refs" / "daft.md").read_text(encoding="utf-8")
    check("T08 A 级含级别行与正文", "- 级别: A(精读)" in qmf_view and "QMF 的框架正文" in qmf_view, qmf_view)
    check("T08 C 级只有总结", "- 级别: C(仅总结)" in daft_view and "## 一句话总结" in daft_view)
    check("T08 C 级不含正文", "UNIQUE-C-BODY-LINE" not in daft_view, daft_view)
    full = json.loads((out / "ref-fulltext.json").read_text(encoding="utf-8"))
    check("T08 全文映射只有 A/B", set(full) == {"yoon", "fungi"}, str(full))
    levels = json.loads((out / "ref-levels.json").read_text(encoding="utf-8"))
    check("T08 ref-levels.json",
          levels == {"convnext": "C", "daft": "C", "fungi": "B", "qmf": "A", "yoon": "A"}, str(levels))

    # T09:工具按 config 指向夹具索引。
    saved = (config.INDEX_DIR, config.REFS_BIB_PATH, config.REFERENCES_ROOT)
    fresh = fixture()
    fresh_out = Path(tempfile.mkdtemp())
    build_reference_index(scan_library(fresh), fresh_out)
    config.INDEX_DIR = fresh_out
    config.REFS_BIB_PATH = fresh / "refs.bib"
    config.REFERENCES_ROOT = fresh
    try:
        from unittest.mock import patch
        with patch("agents.tools._log_lookup") as lookup:
            refused = search_reference_text("daft", "framework")
            check("T09 C 级被拒", refused.startswith("Reference 'daft' is C-level"), refused)
            check("T09 C 级拒绝不调用查阅日志", not lookup.called)
            ok = search_reference_text("yoon", "yoon-unique-term")
            check("T09 A 级返回片段", ok.startswith("# Excerpts"), ok[:200])
            check("T09 A 级查阅日志正向对照", lookup.call_count == 1)
        (fresh_out / "ref-levels.json").unlink()
        fallback = search_reference_text("daft", "framework")
        check("T09 缺登记文件时落到无原文", "has no fulltext available" in fallback, fallback)
    finally:
        config.INDEX_DIR, config.REFS_BIB_PATH, config.REFERENCES_ROOT = saved


def test_empty_and_missing():
    # ① 只有 bib 和空的 A/。
    root = Path(tempfile.mkdtemp())
    w(root / "refs.bib", BIB)
    (root / "A").mkdir()
    lib = scan_library(root)
    check("T10① records 为空", lib.records == {}, str(lib.records))
    check("T10① 只有无笔记警告",
          not lib.errors and lib.warnings and all("没有笔记文件夹" in x for x in lib.warnings),
          str(lib.warnings))
    # ② 三个空级别文件夹,没有 bib。
    root2 = Path(tempfile.mkdtemp())
    for lv in ("A", "B", "C"):
        (root2 / lv).mkdir()
    lib2 = scan_library(root2)
    check("T10② 不报错且恰一条缺 bib 警告",
          not lib2.errors and len(lib2.warnings) == 1 and "没有 refs.bib" in lib2.warnings[0],
          str(lib2.warnings))
    # ③ 没有 bib,但 A/ 里有一份笔记。
    root3 = Path(tempfile.mkdtemp())
    w(root3 / "A" / "x.md", note("Query Matching Fusion", doi="10.1000/qmf"))
    lib3 = scan_library(root3)
    check("T10③ 有材料缺 bib 报错",
          any("1 份文献材料,但缺少 refs.bib" in e for e in lib3.errors), str(lib3.errors))


def test_contracts():
    check("T11 十三类型都有三键",
          all(set(contract_for(t)) == {"focus", "references", "forbidden"}
              and all(contract_for(t).values()) for t in CHAPTER_TYPES))
    check("T11 theory 与 method 是同一对象", contract_for("theory") is contract_for("method"))
    check("T11 unknown 用默认契约", contract_for("unknown") is DEFAULT_CONTRACT)
    check("T11 共享契约不复制",
          CONTRACTS["analysis"] is CONTRACTS["results"] and CONTRACTS["ablation"] is CONTRACTS["results"]
          and CONTRACTS["limitations"] is CONTRACTS["discussion"])

    single = build_contract_clause({"type": "method", "section_types": {}})
    check("T12 单类型开头", single.startswith("CHAPTER CONTRACT (method):"), single[:80])
    check("T12 含 attribution", "attribution" in single)
    multi = build_contract_clause({"type": "mixed", "section_types": {1: "intro", 2: "related", 3: "related"}})
    check("T12 多类型说明", "apply each contract to its own sections only" in multi)
    check("T12 related 只出一段", multi.count("CHAPTER CONTRACT (related)") == 1, multi)

    review = build_contract_review_clause({"type": "method", "section_types": {}})
    check("T13 评审子句", "CHAPTER CONTRACT CHECK (method)" in review)
    check("T13 守卫恰一次", review.count(NOVELTY_GUARD) == 1)
    review2 = build_contract_review_clause({"type": "mixed", "section_types": {1: "intro", 2: "method"}})
    check("T13 多类型守卫仍一次", review2.count(NOVELTY_GUARD) == 1, str(review2.count(NOVELTY_GUARD)))

    abstract = build_contract_clause({"type": "abstract", "section_types": {}})
    check("T14 双花括号保留", "{{RES-xxx}}" in abstract, abstract)


def _prompts_of(chapter_folder: str, keep_input: bool = True):
    """用假 Agent 跑完整流水线,返回 (提示词, 章节目录)。

    keep_input=False 时删掉预置的 input.md,流水线才会真正发出 Stage 0a 备料提示词
    (存在即跳过)。备料要求文献索引存在,这里指一份最小索引。"""
    import agents.outline as ol
    set_idea(IDEA_TEXT)
    folder, outline, _ws = make_full_paper_ws(chapter_folder)
    if not keep_input:
        os.remove(os.path.join(folder, "input.md"))
    index = Path(tempfile.mkdtemp()) / "ref-index.md"
    index.write_text("| key | 级别 | 标题 |\n|---|---|---|\n| qmf | A | Query Matching Fusion |\n",
                     encoding="utf-8")
    saved_index = config.REF_INDEX_PATH
    config.REF_INDEX_PATH = index
    original = ol.OUTLINE_PATH
    ol.OUTLINE_PATH = Path(outline)
    try:
        agent = FullRunAgent(folder)
        import agents.orchestrator as orch
        orch.DATA_ROOT = make_data({"test_accuracy": 0.817})
        orch.run_4stage_with_progress(agent, agent, folder, agent)
    finally:
        ol.OUTLINE_PATH = original
        config.REF_INDEX_PATH = saved_index
    return agent.prompts, folder


def _prompts_of_brief(brief: str):
    """从一份单章 brief 跑完整流水线(FullRunAgent),返回提示词。"""
    import agents.outline as ol
    set_idea(IDEA_TEXT)
    folder, outline, _ws = make_ws_in_outline(brief)
    os.remove(os.path.join(folder, "input.md"))
    index = Path(tempfile.mkdtemp()) / "ref-index.md"
    index.write_text("| key | 级别 |\n|---|---|\n| qmf | A |\n", encoding="utf-8")
    saved_index = config.REF_INDEX_PATH
    config.REF_INDEX_PATH = index
    original = ol.OUTLINE_PATH
    ol.OUTLINE_PATH = Path(outline)
    try:
        agent = FullRunAgent(folder)
        import agents.orchestrator as orch
        orch.DATA_ROOT = make_data(None)
        orch.run_4stage_with_progress(agent, agent, folder, agent)
    finally:
        ol.OUTLINE_PATH = original
        config.REF_INDEX_PATH = saved_index
    return agent.prompts, folder


def test_pipeline_prompts():
    prompts, _folder = _prompts_of("02-method", keep_input=False)
    plan = [p for p in prompts if "acting only as the planner for Stage 1" in p]
    parts = [p for p in prompts if "Write only Part" in p]
    revise = [p for p in prompts if "Resolve EVERY item in this frozen acceptance checklist" in p]
    final = [p for p in prompts if "final.zh.md" in p]
    review = [p for p in prompts if "Write a detailed review" in p]
    verify = [p for p in prompts if "VERIFY mode" in p]
    stage0a = [p for p in prompts if "preparing the source material" in p]
    stage0b = [p for p in prompts if "PRE-DRAFT EVIDENCE MINING" in p]
    check("T15 1a 含契约", plan and all("CHAPTER CONTRACT (method)" in p for p in plan), str(len(plan)))
    check("T15 1b 含契约", parts and all("CHAPTER CONTRACT (method)" in p for p in parts), str(len(parts)))
    check("T15 修订含契约", revise and all("CHAPTER CONTRACT (method)" in p for p in revise), str(len(revise)))
    check("T15 定稿含契约", final and all("CHAPTER CONTRACT (method)" in p for p in final), str(len(final)))
    check("T15 评审含检查", review and all("CHAPTER CONTRACT CHECK (method)" in p for p in review))
    check("T15 备料含规则与表头",
          stage0a and all("REFERENCE RULES FOR THIS CHAPTER" in p
                          and "| key | level | section(s) | use in this chapter |" in p for p in stage0a))
    check("T15 方法章用 Attribution 不用 Novelty",
          stage0b and all("Attribution reviewer" in p and "Novelty reviewer" not in p for p in stage0b))
    check("T15 核验轮至少一条", len(verify) >= 1, str(len(verify)))
    check("T15 核验轮不含契约", all("CHAPTER CONTRACT" not in p for p in verify))

    related, _related_folder = _prompts_of_brief(
        "type: related\n\n1. **Related Work** (~300 words)\n- prior art\n")
    mining = [p for p in related if "PRE-DRAFT EVIDENCE MINING" in p]
    drafting = [p for p in related if "CHAPTER CONTRACT" in p and "Write only Part" in p or
                ("CHAPTER CONTRACT" in p and "acting only as the planner" in p)]
    check("T16 相关工作用 Positioning", mining and all("Positioning reviewer" in p for p in mining))
    check("T16 起草含 section 5",
          any("ONLY from idea.md section 5" in p for p in related), "未见 section 5")
    check("T16 起草侧确有契约", len(drafting) >= 1, str(len(drafting)))


def test_check_and_assemble():
    hits = check_contract("Unlike QMF \\cite{qmf}, our gate learns weights.", ["method"])
    check("T17 对比紧挨引用 → 1 条", len(hits) == 1 and hits[0].startswith("comparative wording"), str(hits))
    check("T17 纯归属 → 0 条",
          check_contract("We use ConvNeXt \\cite{convnext} as the image encoder.", ["method"]) == [])
    check("T17 公式块不算",
          check_contract("$$ unlike \\cite{x} $$", ["method"]) == [])
    cited = check_contract("Prior work shows gains \\cite{qmf}.", ["abstract"])
    check("T17 摘要里的引用 → 1 条", len(cited) == 1 and cited[0].startswith("citation in a abstract"), str(cited))
    check("T17 related 不报", check_contract("Unlike QMF \\cite{qmf}, we differ.", ["related"]) == [])
    skipped = check_contract("Anything \\cite{x}.", ["method", "results"])
    check("T17 多类型跳过", skipped == ["skipped: this run covers several section types"], str(skipped))

    folder = Path(tempfile.mkdtemp())
    missing = write_contract_check(str(folder), "draft-v1.md", ["method"])
    check("T17 源文件不存在不写文件", missing == [] and not (folder / "contract-check.md").exists())

    # T18:组装与指纹。
    from agents.assemble import build_todo_report, load_chapters
    ws = Path(tempfile.mkdtemp())
    chapter = ws / "01-method"
    chapter.mkdir()
    (chapter / "final.md").write_text("Body without gaps.\n", encoding="utf-8")
    (chapter / "verification.md").write_text("Status: VERIFIED\n", encoding="utf-8")
    (chapter / "contract-check.md").write_text(
        "# Chapter contract check (final.md)\nTypes: method\nStatus: 2 hint(s)\n\n"
        "- [CONTRACT] comparative wording next to a citation\n"
        "- [CONTRACT] citation in a method chapter\n"
        "- [CONTRACT] skipped: this run covers several section types\n", encoding="utf-8")
    chs = load_chapters([{"folder": "01-method", "title": "Method", "type": "method", "number": 1}], ws)
    report = build_todo_report(chs, [], __import__("agents.assemble", fromlist=["Numbering"]).Numbering(), {})
    check("T18 两条章节约束", report.count("[章节约束]") == 2, report)
    check("T18 skipped 不进清单", "several section types" not in report, report)
    check("T18 核验状态不变", chs[0].status == "verified" and "核验通过" in report, report)

    import agents.skills_registry as reg
    check("T18 版本号为 3", STAGE_PROMPT_VERSION == 3)
    before = skills_fingerprint()
    copy = Path(tempfile.mkdtemp()) / "chapter_contract.py"
    copy.write_text(reg.CONTRACT_PATH.read_text(encoding="utf-8") + "\n# changed\n", encoding="utf-8")
    saved = reg.CONTRACT_PATH
    reg.CONTRACT_PATH = copy
    try:
        after = skills_fingerprint()
    finally:
        reg.CONTRACT_PATH = saved
    check("T18 契约文件变化改变指纹", before != after, str((before, after)))


def test_new_project_and_rubric():
    root = new_project("demo", Path(tempfile.mkdtemp()))
    refs = root / "inbox" / "references"
    check("T20 三个级别文件夹存在且为空",
          all((refs / lv).is_dir() and not any((refs / lv).iterdir()) for lv in ("A", "B", "C")))
    readme = (root / "inbox" / "说明.md").read_text(encoding="utf-8")
    check("T20 说明含级别文件夹", "A/ B/ C/ 三个级别文件夹" in readme, readme)

    rubric = (ROOT / "skills" / "review-rubric.md").read_text(encoding="utf-8")
    check("T21 不含旧的创新性判据",
          "贡献是否新颖" not in rubric and "换了个说法" not in rubric and "So What" not in rubric)
    check("T21 含不许评判创新性", '不许以"创新性不足"' in rubric)
    check("T21 评审指令不含创新性评判", "Never judge whether the contribution is novel" in REVIEW_INSTRUCTIONS)


if __name__ == "__main__":
    test_scan_and_index()
    test_empty_and_missing()
    test_contracts()
    test_check_and_assemble()
    test_new_project_and_rubric()
    test_pipeline_prompts()
    print(f"\n{len(PASSED)} checks passed.")
