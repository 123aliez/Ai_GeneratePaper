"""材料层单元测试(不调模型):文献室、结果表、代码索引、组装器。

运行: python tests/test_materials.py
"""
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from agents import assemble as A
from agents import results_tables as rt
from agents.code_index import build_code_index
from agents.references import (bib_entry_text, build_reference_index, citable_keys, filter_note,
                               normalize_arxiv, normalize_title, note_summary, parse_bib,
                               parse_front_matter, scan_library, search_fulltext)
from agents.result_ids import ensure_res_ids

PASSED = []


def check(label, cond, detail=""):
    if not cond:
        raise AssertionError(f"{label}: {detail or 'failed'}")
    PASSED.append(label)


def w(path, text, enc="utf-8"):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(text, encoding=enc)


NOTE = """---
title: "Deep Residual Learning for Image Recognition"
doi: "10.1109/CVPR.2016.90"
arxiv: "xxxx.xxxxx"
tags: [cls, 细粒度分类]
---

## 一句话总结
提出残差连接。

## 整体框架
![Figure 1: arch](assets/f.png)
图下的讲解文字保留。

## 局限性
- 论文自己的局限
- 社区讨论中的观点

## 对我的研究的启发
- 私人想法

## 延伸阅读
1. 书单
"""


# ── 文献室 ───────────────────────────────────────────────────────────────
def test_bib_parsing():
    entries, errs = parse_bib(
        '@article{a1, title = {M{\\"u}ller} # { 100\\% data}, year={2020}, eprint={2101.01234}}\n'
        '@misc{B2, title={x @word{y} z}, doi={10.1109/ABC.1}}\n@misc{b2, title={dup}}\n'
        '@comment{ignored @foo{bar,}}\n@misc{nokeyhere}\n')
    check("解析出正常条目,跳过 @comment", set(entries) == {"a1", "B2", "nokeyhere"}, str(list(entries)))
    check("'#' 拼接", "100" in entries["a1"].fields["title"] and "ller" in entries["a1"].fields["title"])
    check("条目内的 @word{ 不被当成新条目", "B2" in entries and "word" not in entries)
    check("重复 key 忽略大小写", any("b2" in e and "重复" in e for e in errs), str(errs))
    check("eprint 取 arXiv 号", entries["a1"].arxiv == "2101.01234")
    check("doi 规范化为小写", entries["B2"].doi == "10.1109/abc.1")
    check("DOI 链接不会被误认成 arXiv 号", normalize_arxiv("https://doi.org/10.1145/3292500.3330701") == "")
    check("TeX 转义题名与 Unicode 题名对号",
          normalize_title('M{\\"u}ller 100\\% data') == normalize_title("Müller 100% data"))
    d = Path(tempfile.mkdtemp(), "refs.bib")
    d.write_text("@article{k1, title={A}}\n@article{k2, title={B}}\n", encoding="utf-8")
    check("按 key 逐字抽取 bib 条目", "k2" in bib_entry_text(d, ["k2"]) and "k1" not in bib_entry_text(d, ["k2"]))


def test_note_parsing_and_filter():
    meta, body = parse_front_matter("\ufeff" + NOTE)
    check("BOM 开头的笔记也能解析", meta.get("doi") == "10.1109/CVPR.2016.90", str(meta))
    check("YAML 列表", meta["tags"] == ["cls", "细粒度分类"])
    check("一句话总结", note_summary(NOTE) == "提出残差连接。", note_summary(NOTE))
    view = filter_note(NOTE)
    check("过滤:去私人小节和延伸阅读", "私人想法" not in view and "书单" not in view)
    check("过滤:去图片行但留讲解文字", "assets/f.png" not in view and "图下的讲解文字保留" in view)
    check("过滤:保留论文自己的局限", "论文自己的局限" in view)
    check("过滤:去 YAML 头", "doi:" not in view)


def test_scan_library_matching_and_errors():
    root = Path(tempfile.mkdtemp())
    w(root / "refs.bib", '@inproceedings{he2016resnet, title={Deep Residual Learning for Image Recognition}, '
                         'year={2016}, doi={10.1109/CVPR.2016.90}}\n@article{orphan, title={No Note Paper}, year={2020}}\n')
    w(root / "resnet" / "任意名字.md", NOTE)
    w(root / "resnet" / "paper.md", "# ResNet\n\nresidual blocks\n\nDepth 152.\n")
    w(root / "bad" / "x.md", "# plain md without YAML head\n")
    lib = scan_library(root)
    check("按 doi 对号(笔记文件名随意)", "he2016resnet" in lib.records and lib.records["he2016resnet"].match == "doi")
    check("原文被识别", lib.records["he2016resnet"].fulltext_path.name == "paper.md")
    check("占位的 arxiv 值被当作空", "arxiv" not in lib.records["he2016resnet"].match)
    check("没有笔记的 bib 条目给提醒", any("orphan" in x for x in lib.warnings), str(lib.warnings))
    check("有 md 却没有笔记的文件夹是错误", any("bad/" in e for e in lib.errors), str(lib.errors))
    out = Path(tempfile.mkdtemp())
    build_reference_index(lib, out)
    idx = (out / "ref-index.md").read_text(encoding="utf-8")
    check("总索引每个 bib 条目一行,无笔记的标明", "he2016resnet" in idx and "orphan" in idx and "(无笔记)" in idx)
    check("全文映射写出", (out / "ref-fulltext.json").is_file())
    hits = search_fulltext((root / "resnet" / "paper.md").read_text(encoding="utf-8"), "residual blocks depth")
    check("原文片段检索", hits and any("residual" in p for _, p in hits))
    check("citable_keys 来自 refs.bib", citable_keys(root / "refs.bib") == {"he2016resnet", "orphan"})
    # 两份笔记 / 重复对号
    w(root / "dup" / "n.md", NOTE)
    lib2 = scan_library(root)
    check("两个文件夹对应同一 bib 条目 → 错误", any("都对应" in e for e in lib2.errors), str(lib2.errors))
    # 缺 bib
    root2 = Path(tempfile.mkdtemp())
    w(root2 / "p" / "n.md", NOTE)
    lib3 = scan_library(root2)
    check("有文献文件夹但缺 refs.bib → 错误", any("refs.bib" in e for e in lib3.errors), str(lib3.errors))


# ── 结果表 ───────────────────────────────────────────────────────────────
def test_results_tables_and_render():
    d = Path(tempfile.mkdtemp())
    w(d / "主结果.csv", "Method,Top-1↑,Top-1_std,Params(M)↓\nResNet-50,76.10,0.30,25.6\nOurs,78.30,0.20,26.0\n")
    w(d / "x.example.csv", "a,b\n1,2\n")
    tables = rt.collect_tables(d)
    check("示例文件被忽略", [t.name for t in tables] == ["主结果"], str([t.name for t in tables]))
    t = tables[0]
    shown, pairs = rt.pair_std_columns(t.cols)
    check("std 列配对并隐藏", pairs == {"Top-1↑": "Top-1_std"} and "Top-1_std" not in shown, str((shown, pairs)))
    md = rt.render_table_markdown(t, "Cap", "note", 1)
    check("± 标准差、最优加粗、原文小数位",
          "**78.30 ± 0.20**" in md and "76.10 ± 0.30" in md and "**25.6**" in md, md)
    tex = rt.render_table_latex(t, "Cap", "n", "tab:x")
    check("LaTeX 表含 booktabs 与 \\pm", "\\toprule" in tex and "$\\pm$" in tex and "\\textbf{25.6}" in tex, tex)
    res_map, path = ensure_res_ids(str(d), str(d / "idx" / "r.md"))
    index = rt.render_data_index(tables, res_map, "实验设置.md")
    check("数据索引每格带 RES 编号与原文", "{{RES-" in index and "=78.30" in index and "实验设置.md" in index, index)
    ph = rt.parse_table_placeholder("主结果 | caption: Main results | note: † from paper")
    check("表格占位符解析", ph == {"name": "主结果", "caption": "Main results", "note": "† from paper"}, str(ph))
    check("未知表名被找出", rt.unknown_tables("[TABLE: 没有 | caption: x]", {"主结果"}) == ["没有"])


# ── 代码索引 ─────────────────────────────────────────────────────────────
def test_code_index_never_executes():
    d = Path(tempfile.mkdtemp())
    marker = Path(tempfile.mkdtemp(), "EXECUTED")
    w(d / "train.py", f'import argparse\nopen(r"{marker}", "w").write("x")\np = argparse.ArgumentParser()\n'
                      f'p.add_argument("--lr", type=float, default=0.1)\nclass Net: pass\nif __name__ == "__main__": pass\n')
    w(d / "bad.py", "def (:\n")
    w(d / "cfg.yaml", "lr: 0.1\n")
    w(d / ".git" / "x.py", "x = 1\n")
    w(d / "w.pt", "binary")
    text = build_code_index(d)
    check("代码没有被执行", not marker.exists())
    check("命令行参数默认值带文件:行号", "train.py:4" in text and "default=0.1" in text, text)
    check("配置文件与坏文件", "cfg.yaml:1" in text and "bad.py" in text and "解析失败" in text, text)
    check("跳过 .git 与权重文件", ".git" not in text.split("## 文件树")[1].split("##")[0] and "w.pt" not in text)
    check("强制标注需核对", "FROM CODE — 需核对" in text)
    check("没有 code 文件夹也不报错", "没有 inbox/code/" in build_code_index(d / "nope"))


# ── 组装器 ───────────────────────────────────────────────────────────────
def test_assembler_pieces():
    d = Path(tempfile.mkdtemp())
    res = d / "results"
    w(res / "主结果.csv", "方法,Top-1↑\nA,1\nB,2\n")
    w(res / "消融.csv", "配置,Top-1↑\nA,1\n")
    w(d / "refs.bib", "@article{he2016, title={Deep}, year={2016}}\n")
    tables = {t.name: t for t in rt.collect_tables(res)}
    chs = [A.Chapter("01-abstract", "Abstract", "abstract", 1,
                     en="We get [MISSING DATA] and [FROM CODE — 需核对] and ⟨未知结果 RES-9⟩.\n"),
           A.Chapter("02-method", "Method", "method", 2,
                     en="# 2. Method\n\n## 2.1 Overview\n\nBuild on \\cite{he2016} \\cite{nokey} a_b & 5% [[TAB:主结果]] [[FIG:ov]].\n\n"
                        "[FIGURE ov: Overview]\nType: diagram\nNote. Modules.\nSource: idea.md 4.1\nSupports: claim\n\n"
                        "[TABLE: 主结果 | caption: M]\n\n[TABLE: 消融 | caption: A]\n\n- one\n- two\n\n$$ E = mc^2 $$\n")]
    chs[1].used = [{"placeholder": "{{DIFF:RES-1,RES-2}}", "ids": ["RES-1", "RES-2"], "text": "-1.0",
                    "kind": "DIFF", "negative": True}]
    num = A.number_items(chs)
    check("表/图按出现顺序编号", num.tables == {"主结果": 1, "消融": 2} and num.figures == {"ov": 1}, str(num))
    md = A.build_paper_md(chs, num, tables)
    check("paper.md:章题归一、交叉引用、图注块、表渲染",
          "## Method" in md and "### Overview" in md and "Table 1" in md and "Figure 1" in md
          and "**Table 1. M**" in md and "待补图" in md, md)
    info = A.build_latex(chs, num, tables, d / "latex", None, "Title", ["A"], d / "refs.bib")
    tex = "\n".join(p.read_text(encoding="utf-8") for p in (d / "latex" / "sections").glob("*.tex"))
    main = (d / "latex" / "main.tex").read_text(encoding="utf-8")
    check("LaTeX:转义、引用、环境",
          "a\\_b \\& 5\\%" in tex and "\\cite{he2016}" in tex and "\\begin{itemize}" in tex
          and "\\[\nE = mc^2\n\\]" in tex, tex)
    check("LaTeX:摘要环境与 \\todo 标记", "\\begin{abstract}" in main and "\\todo{MISSING DATA}" in main
          and "\\todo{unknown result RES-9}" in main, main)
    check("LaTeX:中文表名标签不重复", len(set(__import__("re").findall(r"\\label\{(tab:[^}]+)\}", tex))) == 2, tex)
    check("静态检查报出未知引用 key", any("nokey" in c for c in info["checks"]), str(info["checks"]))
    check("refs.bib 只含被引且存在的条目", "he2016" in (d / "latex" / "refs.bib").read_text(encoding="utf-8"))
    todo = A.build_todo_report(chs, ["提醒一条"], num, tables)
    check("待补清单:标记、负的派生数字、提醒",
          "MISSING DATA" in todo and "FROM CODE" in todo and "为负" in todo and "提醒一条" in todo, todo)
    figs = A.build_figure_report(num, tables, {"02-method": "2. Method"})
    check("图表清单含提示词", "绘图提示词" in figs and "Overview" in figs and "示意图" in figs, figs)
    trace = A.build_trace_report(chs, {"RES-1": {"key": "k1"}, "RES-2": {"key": "k2"}},
                                 {"k1": "主结果.csv:2", "k2": "主结果.csv:3"}, {"主结果": "主结果.csv"})
    check("溯源表:派生数字写明算法与来源", "a − b" in trace and "主结果.csv:2" in trace, trace)


def test_latex_injection_is_neutralized():
    n = A.Numbering()
    out = A._inline_to_tex(r"x \input{/etc/passwd} \write18{ls} \cite{a} \textbf{ok} $\alpha+\input{y}$", n)
    check("危险命令被转义", "\\input{" not in out.replace("\\textbackslash{}input", "") and "\\write18{" not in out, out)
    check("白名单命令仍放行", "\\cite{a}" in out and "\\textbf{ok}" in out, out)
    blk = A.md_to_tex("$$ \\input{/etc/passwd} $$\n\n\\begin{evil}\nx\n\\end{evil}\n\n\\begin{align}\na&=b\n\\end{align}\n", n, {})
    check("危险公式块与未知环境改成 verbatim", blk.count("\\begin{verbatim}") == 2 and "\\begin{align}" in blk, blk)
    tex = A.build_latex([A.Chapter("01-a", "T \\input{/etc/passwd}", "method", 1, en="body\n")], A.number_items([]),
                        {}, Path(tempfile.mkdtemp()), None, "Title \\write18{x}", ["A \\input{y}"], Path("/nonexistent"))
    check("构建过程不抛错", tex["files"])


def test_codex_review_fixes():
    from agents.number_gate import run_number_gate, load_results_store
    from agents.outline import parse_outline
    from agents.outline_expand import validate_translation
    from agents.result_ids import fill_result_refs
    d = Path(tempfile.mkdtemp())
    # 多 seed 未聚合 → 不能整表渲染(不静默取最后一次)
    w(d / "m.csv", "Method,seed,acc\nOurs,0,0.8\nOurs,1,0.9\n")
    t = rt.collect_tables(d)[0]
    check("多 seed 未聚合的表退化为逐项引用", not t.is_grid and len(t.flat) == 2, str(t))
    # BOM:首列 seed 仍被识别
    w(d / "b.csv", "\ufeffseed,model,acc\n0,A,0.8\n1,A,0.9\n", )
    (d / "b.csv").write_bytes("\ufeffseed,model,acc\n0,A,0.8\n1,A,0.9\n".encode("utf-8"))
    keys = [k for k in load_results_store(str(d)) if k.startswith("b.")]
    check("BOM 不会让 seed 变成指标", any("seed=0" in k for k in keys) and not any("ufeff" in k for k in keys), str(keys))
    # 科学计数法差值
    m = {"RES-1": {"key": "a", "value": 1e-3, "text": "1.00e-3"}, "RES-2": {"key": "b", "value": 5e-4, "text": "5.00e-4"}}
    check("科学计数法 DIFF 不再变成 0", fill_result_refs("{{DIFF:RES-1,RES-2}}", m)[0] == "0.000500")
    # 派生数字不被数字门禁当成原始指标
    w(d / "g" / "主结果.csv", "Method,Accuracy\nbase,76.10\nours,78.30\n")
    draft = d / "draft.md"
    draft.write_text("Our accuracy improves by 2.20 points.\n", encoding="utf-8")
    ok0, _ = run_number_gate(str(draft), str(d / "g"))
    ok1, _ = run_number_gate(str(draft), str(d / "g"), exempt_values=[2.2])
    check("派生数字对账:不豁免会误报,豁免后通过", (not ok0) and ok1, str((ok0, ok1)))
    # 过滤:任意层级私人标题
    view = filter_note("## 一句话总结\nx\n### 对我的研究的启发\nSECRET\n#### 子\nS2\n## 局限\nkeep\n")
    check("私人小节按层级过滤", "SECRET" not in view and "S2" not in view and "keep" in view, view)
    # 文献室不跟随符号链接
    root = Path(tempfile.mkdtemp())
    w(root / "refs.bib", "@article{k, title={T}, year={2020}}\n")
    outside = Path(tempfile.mkdtemp()) / "outside.md"
    outside.write_text(NOTE.replace("10.1109/CVPR.2016.90", "x"), encoding="utf-8")
    (root / "p").mkdir()
    os.symlink(outside, root / "p" / "n.md")
    lib = scan_library(root)
    check("指向外部的符号链接笔记不被采用", "k" not in lib.records, str(lib.records))
    # bib 问题
    check("bib 未闭合与垃圾内容报错", parse_bib("@article{k,title={A}")[1] and parse_bib("garbage")[1])
    # 小节 type 在中英文大纲间必须一致
    zh = parse_outline(Path(_o("## 1. A\ntype: method\n### 1.1 X (~100 words)\n- type: experiments\n- p\n")))
    en = parse_outline(Path(_o("## 1. A\ntype: method\n### 1.1 X (~100 words)\n- type: method\n- p\n")))
    check("小节级 type 被改 → 拒绝", any("type" in p for p in validate_translation(zh, en)))
    # 标题归一
    t1, _ = A.normalize_headings("## Only\nBody", "T")
    check("只有一个标题时不删(它是小节)", "Only" in t1)
    t2, _ = A.normalize_headings("# Ch\n```\n# comment\n```\n## Sub\nx", "T")
    check("代码围栏里的 # 不是标题", t2.count("\x00H") == 1, repr(t2))
    # verbatim 不能被内容提前结束
    out = A.md_to_tex("```\n\\end{verbatim}\n\\input{/tmp/p}\n```\n", A.Numbering(), {})
    check("verbatim 不被内容提前结束", out.count("\\end{verbatim}") == 1, out)
    # 非数值单元格原样显示、列序按表头
    d2 = Path(tempfile.mkdtemp())
    w(d2 / "m.csv", "Method,Top-1↑,FLOPs\nResNet,76.1†,4.1\nViT,N/A,17.5\nOurs,78.3,5.0\n")
    md2 = rt.render_table_markdown(rt.collect_tables(d2)[0])
    check("非数值单元格原样显示且列序按表头", "76.1†" in md2 and "N/A" in md2 and md2.index("Top-1") < md2.index("FLOPs"), md2)
    w(d2 / "n.csv", "Name,Score,Time\nA,1,2\nB,3,4\n")
    nt = [t for t in rt.collect_tables(d2) if t.name == "n"][0]
    check("Name,Score,Time 不再被误判成长表", nt.is_grid and "Time" in nt.cols, str(nt.cols))
    a_tex = A.md_to_tex("Intro\n\n$$E=mc^2$$ (1)\n\nNext para\n", A.Numbering(), {})
    check("公式后跟文字不吞后文", "Next para" in a_tex and a_tex.index("Next para") > a_tex.index("\\]"), a_tex)
    figs = A.parse_figure_blocks("[FIGURE f1: Cap]\nType: bar\nNote: n\n\x00H1\x00B\n\ntext")
    check("图块遇到标题行就结束", figs[0]["end"] == 3, str(figs))
    check("$5 and $10 不被当成数学", "\\$5 and \\$10" in A._inline_to_tex("costs $5 and $10 total", A.Numbering()))
    cb = Path(tempfile.mkdtemp(), "c.bib")
    cb.write_text("@inproceedings{p1, title={P}, crossref={conf}}\n@proceedings{conf, title={C}}\n@misc{x, title={X}}\n", encoding="utf-8")
    exp = bib_entry_text(cb, ["p1"])
    check("crossref 依赖条目一并导出", "conf" in exp and "p1" in exp and "@misc" not in exp, exp)
    # 带说明的缺口标记
    check("带说明的缺口标记被识别", A.collect_markers("[REFERENCE NEEDED: more sources]"))


def _o(body):
    p = Path(tempfile.mkdtemp()) / "o.md"
    p.write_text(body, encoding="utf-8")
    return str(p)


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print(f"\nMATERIALS TESTS PASSED — {len(PASSED)} 项检查全过")
