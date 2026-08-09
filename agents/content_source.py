"""内容源抽象 —— 让同一套 Draft/Review 骨架能写综述(证据=文献笔记)或实验论文
(证据=结果库:CSV/JSON/日志/图)。

`PAPER_MODE` 决定后端。本模块只负责"读取并整理原始素材",不再产出聚合文件:
- idea 全文:不再复制进任何 pack,每个 stage 的提示词第一行直接指向全局 idea.md,
  Agent 用 read_file 读原文(见 orchestrator 的 idea_clause 注入)。
- data/ 结果:Manager 在 --init 时按三级索引生成 data/data-index.md 供 data 类章节
  导航(见 build_data_index_prompt);数字门禁仍直读 data/ 原文作为 ground truth。

所以本模块现在只剩两类纯读取/整理函数:idea 相关(load_idea_document / idea_is_skeleton)
与 data 相关(load_results_store / list_plots / render_results_summary)。前者服务于
预检门禁,后者服务于 data-index 生成。
"""
import csv
import json
import os
import re
from pathlib import Path

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from config import PAPER_MODE, DATA_ROOT, REFERENCES_ROOT, IDEA_PATH
from .chapter_type import IDEA, DATA, MIXED


# ── Results store (experiment mode) ─────────────────────────────────────
def _flatten_json(obj, prefix: str = "") -> dict:
    """递归把嵌套 dict/list 压成 {点号键: 数值}。"""
    flat = {}
    if isinstance(obj, dict):
        for key, value in obj.items():
            flat.update(_flatten_json(value, f"{prefix}{key}." if not prefix else f"{prefix}{key}."))
    elif isinstance(obj, list):
        for i, value in enumerate(obj):
            flat.update(_flatten_json(value, f"{prefix}{i}."))
    else:
        try:
            flat[prefix.rstrip(".")] = float(obj)
        except (TypeError, ValueError):
            pass
    return flat


def load_results_store(data_root=None) -> dict:
    """扫 data_root 下 *.json / *.csv,压成 {metric: float}。

    优先委托 number_gate.load_results_store(两者保持一致);失败回退本地实现。
    """
    data_root = str(data_root or DATA_ROOT)
    try:
        from .number_gate import load_results_store as ng_loader
        return ng_loader(data_root)
    except Exception:
        pass
    store = {}
    root = Path(data_root)
    if not root.is_dir():
        return store
    for path in root.rglob("*.json"):
        if ".example." in path.name or path.name.startswith("_template"):
            continue
        try:
            store.update(_flatten_json(json.loads(path.read_text(encoding="utf-8", errors="replace")),
                                       prefix=f"{path.stem}."))
        except Exception:
            continue
    for path in root.rglob("*.csv"):
        if ".example." in path.name or path.name.startswith("_template"):
            continue
        try:
            with open(path, encoding="utf-8", errors="replace", newline="") as handle:
                for row in csv.reader(handle):
                    if len(row) >= 2:
                        try:
                            store[f"{path.stem}.{row[0].strip()}"] = float(row[1])
                        except (ValueError, IndexError):
                            continue
        except Exception:
            continue
    return store


def list_plots(data_root=None) -> list[str]:
    """列 data_root 下可用图文件名(png/pdf/jpg)。"""
    data_root = Path(data_root or DATA_ROOT)
    plots = []
    for pattern in ("*.png", "*.pdf", "*.jpg", "*.jpeg"):
        plots.extend(str(p.relative_to(data_root)) for p in data_root.rglob(pattern))
    return sorted(plots)


def data_dir_has_content(data_root=None) -> bool:
    """data/ 下有没有任何内容(数字结果、文本元数据、图、日志都算)。

    `_maybe_build_data_index` 用它判断是否该跳过 data-index 生成:空目录才跳过。
    注意 load_results_store 只扫 JSON/CSV,所以这里要单独把图和其它文件也算上,
    否则一个只有 experiment_log.md + plot.png 的目录会被误判为空。
    """
    data_root = Path(data_root or DATA_ROOT)
    if not data_root.is_dir():
        return False
    if load_results_store(str(data_root)):
        return True
    if list_plots(str(data_root)):
        return True
    # JSON/CSV 之外的内容(日志 .md/.txt 等):data-index 不解析它们,但它们说明
    # data/ 非空、作者放了东西进来,跳过会在终端产生"目录为空"的误导信息。让 Manager
    # 自己看到这些文件名、决定要不要把它们写进索引。
    for path in data_root.rglob("*"):
        if path.is_file() and not path.name.startswith("."):
            return True
    return False


def render_results_summary(store: dict, plots: list[str] = None,
                           limit: int = 120) -> str:
    """把结果库渲染成三级索引草稿用的 Markdown(数字表 + 文本元数据 + 图清单)。

    这是给 Manager 生成 data-index.md 时的素材概览:Manager 看到这堆原始数字后,
    按"实验名 → 实验结果项 → 具体数值"组织成 data-index.md。这里不替它决定如何分组,
    只把数字与图忠实摊开。

    文本元数据(run_name/description/hardware 等,以 ":" 前缀存)单独列出,因为它
    不是可引用的数字,而是描述"跑了什么"的上下文。
    """
    plots = plots if plots is not None else []
    numeric = {k: v for k, v in store.items() if not (isinstance(v, str) and v.startswith(":"))}
    textual = {k: v[1:] for k, v in store.items() if isinstance(v, str) and v.startswith(":")}

    blocks = []
    if textual:
        blocks.append("## Run metadata (context — NOT numbers to cite)")
        blocks.append("\n".join(f"- **{k}**: {v}" for k, v in sorted(textual.items())))
        blocks.append("")
    if numeric:
        rows = ["| Metric | Value |", "|---|---:|"]
        for key in sorted(numeric)[:limit]:
            rows.append(f"| {key} | {numeric[key]} |")
        if len(numeric) > limit:
            rows.append(f"| … | ({len(numeric) - limit} more) |")
        blocks.append("## Numeric results (the ONLY citable values)")
        blocks.append("\n".join(rows))
    else:
        blocks.append("(no numeric results found in data/ — provide CSV/JSON results first)")
    if plots:
        blocks.append("## Available plots (reference by filename; do not invent figures)")
        blocks.append("\n".join(f"- {p}" for p in plots))
    return "\n\n".join(blocks)


# ── Idea document (read whole by every agent) ───────────────────────────
def load_idea_document(idea_path=None) -> str:
    """读取作者的全局 idea 文档;不存在返回 ""。

    这是所有章节的最高优先输入:贡献、机制、方法设计。故意全文透传而非摘要——
    起草者需要作者自己对贡献的原始表述,在上游改写正是论文论点漂移的开端。
    """
    path = Path(idea_path or IDEA_PATH)
    if not path.is_file():
        return ""
    return path.read_text(encoding="utf-8", errors="replace").strip()


# 作者实际写出的最低词数,低于此值视为"还是没填的模板"。从模板抄来的骨架大部分是
# `>` 提示块和空表格行,剥掉后几乎不剩什么。40 词低到一句逐节简答能过,高到完全没
# 动过的骨架过不了。
_IDEA_MIN_WORDS = 40


def idea_is_skeleton(idea_text: str) -> tuple[bool, int]:
    """检测一份"存在但从未填写"的 idea 文档。

    返回 (是否骨架, 作者实际词数)。

    预检门禁会查 idea.md 是否*存在*;没有这道检查它也会接受一个 100% 模板的文件——
    那是最坏情况,因为运行会继续,起草者把模板自己的问题当成正文内容。所有脚手架
    都剥掉:`>` 提示块、标题、表格分隔线、空表格行、`- $$:` 占位符号,以及尖括号
    占位符 `<...>`(模板里用来标"此处该填什么"的中文指引)。
    """
    if not idea_text:
        return True, 0
    if re.search(r"状态\s*[:：]\s*未填写|status\s*[:：]\s*(not filled|todo|unfilled)",
                 idea_text, re.IGNORECASE):
        return True, 0
    authored = []
    for line in idea_text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith((">", "#", "---")):
            continue                                  # 提示、标题、分隔线
        if set(stripped) <= set("|-: "):
            continue                                  # 表格分隔线 / 空行
        if stripped.startswith("|") and not re.sub(r"[|\s]", "", stripped):
            continue                                  # 空表格行
        if re.fullmatch(r"[-*+]?\s*\$*\s*\$*\s*[:：]?\s*", stripped):
            continue                                  # 没填的 `- $$:` 占位
        if re.fullmatch(r"[-*+]\s*", stripped):
            continue                                  # 空项目符号
        if re.fullmatch(r"\d+\.\s*", stripped):
            continue                                  # 空编号项
        # 去掉行首列表符 / 加粗标签,露出主体再判断占位符
        body = re.sub(r"^[-*+]\s*", "", stripped)
        body = re.sub(r"^\*\*[^*]+\*\*\s*[:：]?\s*", "", body)
        body = body.strip()
        # 整行被单个 <...> 包住 → 占位符,跳过
        if re.fullmatch(r"<[^<>]*>", body):
            continue
        # 行内 <...> 片段(如 - **xx**:<填这里>)抠掉再判断是否全空
        body = re.sub(r"<[^<>]*>", "", body).strip(" -|:")
        if not body:
            continue                                  # 抠掉占位符后整行空
        authored.append(stripped)
    words = len(" ".join(authored).split())
    # 中文几乎没有空格,把 CJK 字符也算作词。
    words += len(re.findall(r"[一-鿿]", " ".join(authored)))
    return words < _IDEA_MIN_WORDS, words


def content_source_summary(family: str = "") -> str:
    """运行横幅用的单行素材状态摘要。"""
    if PAPER_MODE != "experiment":
        return f"survey mode — reference notes in {REFERENCES_ROOT}"
    numeric = {k: v for k, v in load_results_store().items()
               if not (isinstance(v, str) and v.startswith(":"))}
    idea = load_idea_document()
    idea_state = f"idea.md {len(idea.split())} words" if idea else "idea.md MISSING"
    parts = [f"experiment mode — {idea_state}", f"{len(numeric)} metrics in {DATA_ROOT.name}/"]
    if family:
        parts.append(f"routing={family}")
    return " — ".join(parts[:1]) + ", " + ", ".join(parts[1:])


if __name__ == "__main__":
    print("PAPER_MODE:", PAPER_MODE)
    print("summary:", content_source_summary())
    # idea 相关函数自检:填了的 idea 不算骨架,没填的算。
    filled = "# Idea\n\n## 贡献\n我们提出 Spec 模块,提升细粒度分类精度。\n"
    assert not idea_is_skeleton(filled)[0], "填了的 idea 不应被判为骨架"
    assert idea_is_skeleton("")[0], "空文档应判为骨架"
    # render_results_summary 把数字与图忠实摊开(不替 Manager 决定如何分组)。
    summary = render_results_summary({"run_0.accuracy": 0.817, "run_0.loss": 0.12},
                                     plots=["plot.png"])
    assert "0.817" in summary and "plot.png" in summary, summary
    print("\nSELF-TEST PASSED: content_source readers work without any context pack.")
