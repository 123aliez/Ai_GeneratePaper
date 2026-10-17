"""项目模式端到端(假模型,不调 API):--new → --check → --go(停在大纲)→ 审 → --go(出交付物)。

覆盖:建项目与模板、材料检查、三份索引、大纲首次展开即停、idea 改动提醒/--keep-outline、
中文大纲改动后翻译、逐章生成、整篇组装(paper.md / LaTeX / 待补清单 / 图表清单 / 数字溯源表)、
手改终稿后 --assemble 重新核验。

运行: python tests/test_project_flow.py
"""
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PASSED = []


def check(label, cond, detail=""):
    if not cond:
        raise AssertionError(f"{label}: {detail or 'failed'}")
    PASSED.append(label)


ENV = {**os.environ, "DRAFT_API_KEY": "k", "DRAFT_API_BASE": "http://x", "REVIEW_API_KEY": "k",
       "REVIEW_API_BASE": "http://x", "MANAGER_API_KEY": "k", "MANAGER_API_BASE": "http://x",
       "PYTHONDONTWRITEBYTECODE": "1"}


def run(*args, project=None, driver=True):
    """运行 run.py(或带假模型的驱动)。返回 (returncode, stdout+stderr)。"""
    if driver:
        cmd = [sys.executable, "-B", str(ROOT / "tests" / "e2e_driver.py"), str(project), *args]
    else:
        cmd = [sys.executable, "-B", str(ROOT / "run.py"), *args]
    p = subprocess.run(cmd, cwd=str(ROOT), env=ENV, capture_output=True, text=True, timeout=600)
    return p.returncode, p.stdout + p.stderr


IDEA = """# Idea — Spec

## 1. 一句话贡献
我们提出 Spec 模块，一种即插即用的谱域重标定单元，在几乎不增加参数的前提下提升细粒度分类精度。

## 3. 核心洞察
细粒度类别的判别信息集中在中高频段，在频域重标定通道可以放大这部分信号而不是把它平均掉。

## 4. 方法设计
g(x) = sigmoid(W2 relu(W1 F(x)))，先做 FFT，再逐通道重标定，最后逆变换回空间域并乘回特征图。
"""

NOTE = """---
title: "Deep Residual Learning for Image Recognition"
doi: "10.1109/CVPR.2016.90"
tags:
  - cls
---

## 一句话总结
提出残差连接，解决深层网络训练退化问题。

## 局限性
- 论文自身提到的边界。

## 对我的研究的启发
- 私人想法，不能给模型看。
"""


def make_project(tmp: Path, name="demo") -> Path:
    rc, out = run("--new", name, "--parent", str(tmp), driver=False)
    check("--new 成功", rc == 0, out)
    root = tmp / name
    inbox = root / "inbox"
    for sub in ("results", "references", "code", "latex-template"):
        check(f"建了 inbox/{sub}", (inbox / sub).is_dir())
    check("模板: idea / 骨架 / 设置 / 笔记模板 / 说明",
          all(p.is_file() for p in (inbox / "idea.md", inbox / "outline_draft.md",
                                    inbox / "results" / "实验设置.md",
                                    inbox / "references" / "笔记模板.md", inbox / "说明.md")))
    rc2, out2 = run("--new", name, "--parent", str(tmp), driver=False)
    check("已存在的项目不覆盖", rc2 == 1 and "已存在" in out2, out2)
    (inbox / "idea.md").write_text(IDEA, encoding="utf-8")
    (inbox / "outline_draft.md").write_text(
        "| # | 章标题 | type |\n|---|---|---|\n| 1 | Abstract | abstract |\n| 2 | Method | method |\n"
        "| 3 | Results | results |\n| 4 | Conclusion | conclusion |\n", encoding="utf-8")
    (inbox / "results" / "主结果.csv").write_text(
        "Method,Top-1↑,Top-1_std,Params(M)↓\nResNet-50,76.10,0.30,25.6\nOurs,78.30,0.20,26.0\n", encoding="utf-8")
    (inbox / "results" / "实验设置.md").write_text("# 实验设置\n\n## 数据集\nImageNet-1k，标准划分。\n", encoding="utf-8")
    (inbox / "references" / "refs.bib").write_text(
        "@inproceedings{he2016resnet, title={Deep Residual Learning for Image Recognition}, "
        "booktitle={CVPR}, year={2016}, doi={10.1109/CVPR.2016.90}}\n", encoding="utf-8")
    ref = inbox / "references" / "resnet"
    ref.mkdir()
    (ref / "笔记.md").write_text(NOTE, encoding="utf-8")
    (ref / "paper.md").write_text("# ResNet\n\nWe use residual blocks.\n\nDepth up to 152 layers.\n", encoding="utf-8")
    (inbox / "code" / "train.py").write_text(
        "import argparse\np = argparse.ArgumentParser()\np.add_argument('--lr', type=float, default=0.1)\n",
        encoding="utf-8")
    return root


def test_flow():
    tmp = Path(tempfile.mkdtemp())
    root = make_project(tmp)

    rc, out = run("--check", project=root)
    check("材料检查通过", rc == 0, out)
    check("检查报告写到 output/", (root / "output" / "材料检查.md").is_file())

    rc, out = run("--go", project=root)
    check("首次 --go 展开大纲后停下(退出码 0)", rc == 0 and "outline.zh.md" in out, out[-600:])
    zh = root / "outline.zh.md"
    check("生成了中文大纲和英文大纲", zh.is_file() and (root / "_work" / "outline.md").is_file())
    check("三份索引已建",
          all((root / "_work" / "index" / n).is_file() for n in ("ref-index.md", "data-index.md", "res-ids.md", "code-index.md")))
    check("文献索引含 key 与一句话总结",
          "he2016resnet" in (root / "_work" / "index" / "ref-index.md").read_text(encoding="utf-8")
          and "残差连接" in (root / "_work" / "index" / "ref-index.md").read_text(encoding="utf-8"))
    note_view = (root / "_work" / "index" / "refs" / "he2016resnet.md").read_text(encoding="utf-8")
    check("过滤版笔记不含私人小节", "私人想法" not in note_view and "残差连接" in note_view)
    check("没有开始逐章生成", not (root / "_work" / "workspace").exists()
          or not any(p.is_dir() for p in (root / "_work" / "workspace").iterdir()))

    # idea 改动 → 提醒并停下
    idea = root / "inbox" / "idea.md"
    idea.write_text(IDEA + "\n## 5. 补充\n这是展开大纲之后新增的内容，用来触发提醒。\n", encoding="utf-8")
    rc, out = run("--go", project=root)
    check("idea 改过 → 提醒并以非 0 退出", rc == 1 and "idea.md" in out and "--keep-outline" in out, out[-500:])
    idea.write_text(IDEA, encoding="utf-8")

    # 作者审大纲:改中文大纲 → --go 触发翻译,然后逐章生成并组装
    zh.write_text(zh.read_text(encoding="utf-8") + "\n", encoding="utf-8")
    rc, out = run("--go", project=root)
    check("第二次 --go 跑完", rc == 0, out[-1500:])
    check("检测到中文大纲改动并翻译", "翻译完成" in out, out[-800:])

    o = root / "output"
    for name in ("paper.md", "paper.zh.md", "待补清单.md", "图表清单.md", "数字溯源表.md"):
        check(f"交付物 {name}", (o / name).is_file())
    for name in ("main.tex", "refs.bib"):
        check(f"LaTeX 工程 {name}", (o / "latex" / name).is_file())

    paper = (o / "paper.md").read_text(encoding="utf-8")
    check("结果表按 CSV 渲染且加粗最优、拼 ± 标准差", "**78.30 ± 0.20**" in paper and "76.10 ± 0.30" in paper, paper)
    check("占位符替换为带原文小数位的值", "78.30%" in paper, paper)
    check("DIFF 派生数字由程序算出(2.20)", "2.20 points" in paper, paper)
    check("REL 派生数字(2.9)", "2.9%" in paper, paper)
    check("交叉引用换成编号", "Table 1" in paper and "Figure 1" in paper and "[[" not in paper, paper)
    check("图占位块变成图注", "Figure 1. Architecture overview" in paper)

    tex = (o / "latex" / "sections" / "03-method.tex").read_text(encoding="utf-8") if (
        o / "latex" / "sections" / "03-method.tex").exists() else "\n".join(
        p.read_text(encoding="utf-8") for p in (o / "latex" / "sections").glob("*.tex"))
    check("LaTeX 保留 \\cite 并带 \\todo 标记", "\\cite{he2016resnet}" in tex and "\\todo{FROM CODE" in tex, tex[:600])
    check("refs.bib 只含被引条目", "he2016resnet" in (o / "latex" / "refs.bib").read_text(encoding="utf-8"))
    main = (o / "latex" / "main.tex").read_text(encoding="utf-8")
    check("main.tex 含摘要与章节 input", "\\begin{abstract}" in main and "\\input{sections/" in main, main)

    todo = (o / "待补清单.md").read_text(encoding="utf-8")
    check("待补清单列出来自代码的细节", "FROM CODE" in todo, todo)
    figs = (o / "图表清单.md").read_text(encoding="utf-8")
    check("图表清单含绘图提示词", "绘图提示词" in figs and "Architecture overview" in figs, figs)
    trace = (o / "数字溯源表.md").read_text(encoding="utf-8")
    check("溯源表指到 CSV 行", "主结果.csv:3" in trace and "主结果.Ours.Top-1↑" in trace, trace)

    # 手改终稿:制造数字缺口 → --assemble 重新核验,待补清单里标出未核验
    res_dir = root / "_work" / "workspace"
    result_dir = next(p for p in res_dir.iterdir() if p.is_dir() and "result" in p.name)
    final = result_dir / "final.md"
    final.write_text(final.read_text(encoding="utf-8") + "\nAlso ⟨未知结果 RES-999⟩ was hand-edited in.\n", encoding="utf-8")
    rc, out = run("--assemble", project=root)
    check("--assemble 成功且不调模型", rc == 0, out[-600:])
    todo2 = (o / "待补清单.md").read_text(encoding="utf-8")
    check("手改后重新核验:未核验 + 点名未知结果", "未核验" in todo2 and "RES-999" in todo2, todo2)
    vtext = (result_dir / "verification.md").read_text(encoding="utf-8")
    check("verification.md 更新为 UNVERIFIED", "Status: UNVERIFIED" in vtext, vtext)


def test_requires_project():
    rc, out = run("--go", driver=False)
    check("不带 --project → 报错", rc == 2 and "--project" in out, out)
    tmp = Path(tempfile.mkdtemp())
    rc, out = run("--project", str(tmp), "--list", "--no-such-flag", driver=False)
    check("未知参数被拒绝", rc == 2 and "未知参数" in out, out)
    rc, out = run("--project", str(tmp), "--go", "--list", driver=False)
    check("多个动作互斥", rc == 2, out)


if __name__ == "__main__":
    test_requires_project()
    test_flow()
    print(f"\nPROJECT FLOW TESTS PASSED — {len(PASSED)} 项检查全过")
