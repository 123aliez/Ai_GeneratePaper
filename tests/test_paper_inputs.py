"""论文输入通道、附录与外部数字归属的离线回归。"""
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agents import assemble as A, results_tables as rt
from agents.chapter_type import normalize_type, route_for_type, MIXED, ADVISORY
from agents.chapter_contract import CONTRACTS
from agents.draft_checks import methodology_findings
from agents.number_gate import check_numbers_against_results
from agents.outline import plan_chapter_words
from agents.result_ids import ensure_res_ids

PASSED = []


def check(name, condition):
    assert condition, name
    PASSED.append(name)


def test_reference_only():
    root = Path(tempfile.mkdtemp())
    (root / "main.csv").write_text("Method,Accuracy\nOurs,0.9\n", encoding="utf-8")
    (root / "ref_tests.csv").write_text("Method,p_value\nOurs,0.0048\n", encoding="utf-8")
    tables = {t.name: t for t in rt.collect_tables(root)}
    res, _ = ensure_res_ids(str(root), root / "res-ids.md")
    check("数据索引保留辅助数值", "0.0048" in rt.render_data_index(list(tables.values()), res))
    check("可插表目录过滤 ref_", rt.table_names(root) == ["main"])
    check("辅助数据明确标记仅引用数字", tables["ref_tests"].reference_only)
    marker = "[TABLE: ref_tests | caption: Paired test results]\n\nThe comparison is significant."
    check("起草检查拒绝辅助插表", any("reference-only" in x for x in rt.table_style_warnings(marker, tables)))
    check("辅助数据不占论文表号", not A.number_items(
        [A.Chapter("results", "Results", "results", number=1, en=marker)]).tables)
    for render in (rt.render_table_markdown, rt.render_table_latex):
        try:
            render(tables["ref_tests"])
            check("渲染拒绝辅助插表", False)
        except ValueError:
            check("渲染拒绝辅助插表", True)
    check("辅助数据 RES 编号保留", any("ref_tests" in item["key"] for item in res.values()))


def test_attributed_numbers():
    results = {"ours.Accuracy": 0.9}
    prior = r"QMF reported accuracy 69.14\% \cite{zhang2023qmf}."
    check("原文数字不会对账本文结果", check_numbers_against_results(
        prior, results, prior_citation_keys={"zhang2023qmf"})["checked"] == 0)
    ours = r"We reported accuracy 69.14\% \cite{zhang2023qmf}."
    check("本文数字加引用也必须对账", not check_numbers_against_results(
        ours, results, prior_citation_keys={"zhang2023qmf"})["ok"])
    third_person = r"HAF achieved accuracy 69.14\% \cite{zhang2023qmf}."
    check("第三人称本文方法也不能借引用绕过", not check_numbers_against_results(
        third_person, results, prior_citation_keys={"zhang2023qmf"})["ok"])
    check("未知引用不能豁免", not check_numbers_against_results(
        prior, results, prior_citation_keys={"unknown"})["ok"])
    mixed = prior + r" However, our accuracy is 69.14\%."
    check("混合句仍检查本文结果", not check_numbers_against_results(
        mixed, results, prior_citation_keys={"zhang2023qmf"})["ok"])
    check("相关工作不开放数字豁免", "numbers reported by other papers" in CONTRACTS["related"]["forbidden"])


def test_figures_template_appendix():
    root = Path(tempfile.mkdtemp())
    source, template, out = root / "figures", root / "template", root / "out"
    source.mkdir()
    template.mkdir()
    (source / "overview.pdf").write_bytes(b"%PDF-test-fixture")
    (template / "README.md").write_text("template instructions", encoding="utf-8")
    (template / "nested").mkdir()
    (template / "nested" / "helper.sty").write_text("% package", encoding="utf-8")
    (template / "main.tex").write_text(
        "\\documentclass{article}\n\\begin{document}\n% PAPER_BODY\n\\end{document}", encoding="utf-8")
    chapters = [A.Chapter("01-method", "Method", "method", number=1,
                          en="[FIGURE overview: Framework overview]\nNote. Our framework.\n\nText."),
                A.Chapter("02-appendix", "Additional evidence", "appendix", number=2,
                          en="## Supplied proof\n\n\\begin{proof}\nThe supplied argument.\n\\end{proof}",
                          sections=["Supplied proof"])]
    num = A.number_items(chapters)
    A.bind_input_figures(num, source)
    bib = root / "refs.bib"
    bib.write_text("", encoding="utf-8")
    A.build_latex(chapters, num, {}, out, template, "Title", [], bib)
    copied = out / "figures" / "fig1-overview.pdf"
    check("图标识自动映射编号文件", copied.read_bytes() == (source / "overview.pdf").read_bytes())
    check("模板说明文件不复制", not (out / "README.md").exists())
    check("模板依赖保留目录", (out / "nested" / "helper.sty").exists())
    md = A.build_paper_md(chapters, num, {}, "en")
    check("已提供图无待补标记", "待补图" not in md and "latex/figures/fig1-overview.pdf" in md)
    check("附录字母编号", num.sections["additional evidence"] == "A"
          and num.sections["supplied proof"] == "A.1")
    main = (out / "main.tex").read_text()
    check("附录通过标准 LaTeX 入口", r"\appendix" in main and r"\usepackage{amsthm}" in main)
    check("附录允许已有证明", not methodology_findings(r"\begin{proof}A.\end{proof}", ["appendix"]))
    check("正文继续禁止证明", bool(methodology_findings(r"\begin{proof}A.\end{proof}", ["method"])))
    (source / "overview.pdf").unlink()
    (source / "overview.png").write_bytes(b"png-fixture")
    num.figure_assets.clear()
    A.bind_input_figures(num, source)
    A.build_latex(chapters, num, {}, out, template, "Title", [], bib)
    check("更换输入格式后删除旧格式缓存", not copied.exists() and copied.with_suffix(".png").exists())
    shifted = A.number_items([A.Chapter("00", "Intro", "intro", number=0,
                                       en="[FIGURE first: First image]\n\nText.")] + chapters)
    A.bind_input_figures(shifted, source)
    A.build_latex(chapters, shifted, {}, out, template, "Title", [], bib)
    check("改变图号仍找到原始图标识", (out / "figures" / "fig2-overview.png").exists())


def test_appendix_budget_and_abstract():
    check("附录类型与别名", normalize_type("附录") == "appendix"
          and route_for_type("appendix") == (MIXED, ADVISORY))
    section = dict(number=1, title="Study", target_words=500, words_explicit=False, bullets=[])
    main = dict(number=1, type="method", sections=[section])
    extra = dict(number=2, type="appendix", sections=[section.copy()])
    check("附录不挤占正文字数预算", plan_chapter_words([main], 10, 650)[1]
          == plan_chapter_words([main, extra], 10, 650)[1])
    check("摘要中段明确用三至四句", "3-4 middle sentences" in CONTRACTS["abstract"]["focus"])


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print(f"{len(PASSED)} checks passed.")
