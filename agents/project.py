"""项目管理:建项目、材料检查、建索引、大纲状态、整篇组装入口。

一篇论文 = 一个项目文件夹(见 config.py 顶部的布局说明)。本模块不调模型——
需要模型的步骤(展开大纲、逐章生成)由 run.py 编排。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
TEMPLATES = REPO_ROOT / "templates"
STATE_NAME = "outline-state.json"

INBOX_README = """# inbox/ —— 你的全部材料放这里(系统只读)

| 位置 | 放什么 |
|---|---|
| `idea.md` | 创新点文档(按模板填;最重要) |
| `outline_draft.md` | 章节骨架表(几章、每章叫什么、什么 type) |
| `results/` | 每个 CSV 是论文里的一张结果表 + `实验设置.md`。第一列是方法名,`X_std` 列是 X 的标准差,列名带 ↑/↓ 表示越大/越小越好;**表头会原样出现在论文里,建议用英文** |
| `references/` | `refs.bib`(总 bib)+ 每篇文献一个文件夹(笔记 md + 原文 md,笔记按 `笔记模板.md` 写) |
| `code/` | 实验代码(整个文件夹拖进来;只做静态解析,不执行) |
| `latex-template/` | 可选。会议模板(含 `main.tex`,正文位置写一行 `% PAPER_BODY`);不放则用内置模板 |

流程:填好材料 → `python run.py --project <项目> --go`(展开大纲后停下)→ 审 `../outline.zh.md`
→ 再跑一次 `--go`(逐章生成并组装到 `../output/`)。
"""


def sha_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def sha_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()[:16] if path.is_file() else ""


# ── 建项目 ───────────────────────────────────────────────────────────────
def new_project(name: str, parent: Path | None = None) -> Path:
    """建项目文件夹和模板。目标已存在且非空 → 抛 FileExistsError(不覆盖)。"""
    if not name or any(ch in name for ch in '/\\:*?"<>|') or name in (".", ".."):
        raise ValueError(f"项目名不合法: {name!r}")
    root = (Path(parent) if parent else REPO_ROOT / "projects") / name
    if root.exists() and any(root.iterdir()):
        raise FileExistsError(f"{root} 已存在且非空")
    inbox = root / "inbox"
    for sub in ("results", "references", "code", "latex-template"):
        (inbox / sub).mkdir(parents=True, exist_ok=True)
    shutil.copy2(TEMPLATES / "idea.md", inbox / "idea.md")
    shutil.copy2(TEMPLATES / "outline_draft.md", inbox / "outline_draft.md")
    for f in (TEMPLATES / "results").iterdir():
        shutil.copy2(f, inbox / "results" / f.name)
    for f in (TEMPLATES / "references").iterdir():
        shutil.copy2(f, inbox / "references" / f.name)
    shutil.copy2(TEMPLATES / "paper.toml", root / "paper.toml")
    (inbox / "说明.md").write_text(INBOX_README, encoding="utf-8")
    return root


# ── 状态 ─────────────────────────────────────────────────────────────────
def state_path() -> Path:
    import config
    return Path(config.WORK_ROOT) / STATE_NAME


def load_state() -> dict:
    p = state_path()
    try:
        return json.loads(p.read_text(encoding="utf-8")) if p.is_file() else {}
    except (OSError, ValueError):
        return {}


def save_state(state: dict) -> None:
    p = state_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(f".{p.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, p)


def input_shas() -> dict:
    """展开大纲时所依据的作者输入的指纹:idea.md 与骨架表。"""
    import config
    from .outline import OUTLINE_DRAFT_PATH
    return {"idea": sha_file(Path(config.IDEA_PATH)), "draft": sha_file(Path(OUTLINE_DRAFT_PATH))}


def record_expansion() -> None:
    """--expand 成功后记账:此刻的 idea / 骨架 / 中文大纲指纹。"""
    from .outline import OUTLINE_ZH_PATH
    st = load_state()
    st.update(input_shas())
    st["zh"] = sha_file(Path(OUTLINE_ZH_PATH))
    save_state(st)


def outline_inputs_changed() -> list[str]:
    """idea.md / 骨架表自上次展开后变过吗?返回变化项名称列表。"""
    st = load_state()
    now = input_shas()
    return [label for key, label in (("idea", "idea.md"), ("draft", "outline_draft.md"))
            if st.get(key) and st.get(key) != now[key]]


# ── 材料检查 ─────────────────────────────────────────────────────────────
@dataclass
class Check:
    errors: list = field(default_factory=list)
    warnings: list = field(default_factory=list)
    info: list = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors

    def render(self) -> str:
        out = ["# 材料检查(程序生成)", ""]
        if self.errors:
            out += ["## 必须先修(--go 在这里停下)", ""] + [f"- {e}" for e in self.errors] + [""]
        if self.warnings:
            out += ["## 提醒(不阻塞)", ""] + [f"- {w}" for w in self.warnings] + [""]
        if self.info:
            out += ["## 概况", ""] + [f"- {i}" for i in self.info] + [""]
        if not (self.errors or self.warnings):
            out += ["材料齐全,没有问题。", ""]
        return "\n".join(out)


def _settings_unfilled(text: str) -> bool:
    """实验设置.md 还是模板(每个小节下只有 <...> 提示)?"""
    body = [ln.strip() for ln in text.splitlines()
            if ln.strip() and not ln.strip().startswith(("#", ">"))]
    if not body:
        return True
    return all(re.fullmatch(r"<.*>", ln) for ln in body)


def check_materials(need_models: bool = False) -> Check:
    import config
    from .content_source import idea_is_skeleton, load_idea_document
    from .number_gate import load_results_store_report, format_store_issues
    from .outline import OUTLINE_DRAFT_PATH, OUTLINE_ZH_PATH, OutlineRouteError, parse_outline
    from .references import scan_library
    from .results_tables import collect_tables

    chk = Check()
    inbox = Path(config.INBOX_ROOT)
    if not inbox.is_dir():
        chk.errors.append(f"找不到 {inbox}(先 `python run.py --new 名字` 建项目)")
        return chk

    # idea
    idea_path = Path(config.IDEA_PATH)
    if not idea_path.is_file():
        chk.errors.append("缺少 inbox/idea.md(贡献的唯一来源,不能省)")
    else:
        skeleton, words = idea_is_skeleton(load_idea_document())
        if skeleton:
            chk.errors.append(f"inbox/idea.md 还是没填的模板(只有 {words} 词作者内容)")
        else:
            chk.info.append(f"idea.md: {words} 词")

    # 大纲骨架
    draft = Path(OUTLINE_DRAFT_PATH)
    chapters = []
    if draft.is_file():
        try:
            chapters = parse_outline(draft)
            if not chapters:
                chk.errors.append("inbox/outline_draft.md 里没解析出任何章节(需要一张 # | 章标题 | type 的表)")
            else:
                chk.info.append(f"大纲骨架: {len(chapters)} 章")
        except OutlineRouteError as exc:
            chk.errors.append(f"inbox/outline_draft.md 解析失败: {exc}")
    elif not Path(OUTLINE_ZH_PATH).is_file():
        chk.errors.append("缺少 inbox/outline_draft.md(章节骨架表,你手填)")

    # 结果表
    results = Path(config.DATA_ROOT)
    store, issues = load_results_store_report(str(results))
    if issues["duplicates"] or issues["unreadable"]:
        chk.errors.append("结果表有问题:\n    " + format_store_issues(issues).replace("\n", "\n    "))
    tables = collect_tables(results)
    numeric = {k: v for k, v in store.items() if isinstance(v, (int, float))}
    chk.info.append(f"结果表: {len(tables)} 张,{len(numeric)} 个数值")
    for t in tables:
        if t.collided:
            chk.warnings.append(f"结果表 `{t.name}` 里同一个 (行名, 列名) 有多个值(多 seed 未聚合?),"
                                f"不能用 [TABLE] 整表渲染,只能逐个引用数字;要整表渲染请先聚合成均值 ± 标准差")
        elif not t.is_grid:
            chk.warnings.append(f"结果表 `{t.name}` 第一列不是方法名(或没有行名),"
                                f"不能用 [TABLE] 整表渲染,只能逐个引用数字")
        if t.text_cells:
            chk.warnings.append(f"结果表 `{t.name}` 有 {len(t.text_cells)} 个非数值单元格"
                                f"(如 {next(iter(t.text_cells.values()))!r}):表里原样显示,"
                                f"但不编 RES 编号、正文不能引用它的数值")
    from .chapter_type import route_for_type, BLOCKING
    blocking = [c["title"] for c in chapters if route_for_type(c.get("type", ""))[1] == BLOCKING]
    if blocking and not numeric:
        chk.errors.append(f"大纲里有必须以结果表为依据的章({', '.join(blocking)}),"
                          f"但 inbox/results/ 里没有结果表")
    settings = results / config.SETTINGS_FILENAME
    if not settings.is_file():
        chk.warnings.append(f"缺少 inbox/results/{config.SETTINGS_FILENAME}:实验设置章只能标 [UNKNOWN]")
    elif _settings_unfilled(settings.read_text(encoding="utf-8", errors="replace")):
        chk.warnings.append(f"inbox/results/{config.SETTINGS_FILENAME} 还是模板,没填内容")

    # 文献室
    lib = scan_library(config.REFERENCES_ROOT, config.REFS_BIB_PATH)
    chk.errors.extend(f"文献室: {e}" for e in lib.errors)
    chk.warnings.extend(f"文献室: {w}" for w in lib.warnings)
    chk.info.append(f"文献: refs.bib {len(lib.entries)} 条,配到笔记 {len(lib.records)} 篇")

    # 代码 / 模板 / paper.toml
    code = Path(config.CODE_ROOT)
    if code.is_dir() and any(code.iterdir()):
        chk.info.append("实验代码: 将建静态索引(只解析,不执行)")
    tpl = Path(config.TEMPLATE_ROOT)
    if tpl.is_dir() and any(tpl.iterdir()):
        if not (tpl / "main.tex").is_file():
            chk.warnings.append("inbox/latex-template/ 里没有 main.tex:将使用内置模板")
        elif "% PAPER_BODY" not in (tpl / "main.tex").read_text(encoding="utf-8", errors="replace"):
            chk.warnings.append("latex-template/main.tex 里没有 `% PAPER_BODY` 行:章节会插在 \\end{document} 前")
    toml_path = Path(config.PROJECT_ROOT) / "paper.toml"
    if toml_path.is_file():
        try:
            cfg = tomllib.loads(toml_path.read_text(encoding="utf-8"))
            if "title" in cfg and not isinstance(cfg["title"], str):
                chk.errors.append("paper.toml: title 必须是字符串")
            if "authors" in cfg and not (isinstance(cfg["authors"], list)
                                         and all(isinstance(a, str) for a in cfg["authors"])):
                chk.errors.append('paper.toml: authors 必须是字符串列表,如 ["A", "B"]')
        except tomllib.TOMLDecodeError as exc:
            chk.errors.append(f"paper.toml 解析失败: {exc}")

    if need_models:
        for role in ("DRAFT", "REVIEW", "MANAGER"):
            missing = [n for n in ("MODEL", "API_KEY", "API_BASE")
                       if not getattr(config, f"{role}_{n}", "")]
            if missing:
                chk.errors.append(f".env 里 {role}_{'/'.join(missing)} 没填")
    return chk


# ── 索引 ─────────────────────────────────────────────────────────────────
def _write_if_changed(path: Path, text: str) -> None:
    if path.is_file() and path.read_text(encoding="utf-8", errors="replace") == text:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def build_all_indexes() -> dict:
    """建文献索引 / 代码索引 / 数据索引(全部确定性,不调模型)。返回概况。"""
    import config
    from .code_index import build_code_index
    from .references import build_reference_index, scan_library
    from .result_ids import ensure_res_ids
    from .paths import res_catalog_path
    from .results_tables import collect_tables, render_data_index

    info = {}
    lib = scan_library(config.REFERENCES_ROOT, config.REFS_BIB_PATH)
    build_reference_index(lib, config.INDEX_DIR)
    info["references"] = len(lib.entries)
    _write_if_changed(Path(config.CODE_INDEX_PATH), build_code_index(config.CODE_ROOT))
    res_map, path = ensure_res_ids(str(config.DATA_ROOT), res_catalog_path(config.DATA_ROOT))
    info["results"] = len(res_map)
    settings = Path(config.DATA_ROOT) / config.SETTINGS_FILENAME
    # 没有结果时也要写(空)索引:否则旧索引会原样留着,混合章还会被提示去读它。
    _write_if_changed(Path(config.DATA_INDEX_PATH), render_data_index(
        collect_tables(config.DATA_ROOT) if path is not None else [], res_map,
        settings.name if settings.is_file() else ""))
    return info


# ── 组装 ─────────────────────────────────────────────────────────────────
def assemble_output(extra_notes: list[str] | None = None) -> dict:
    """把各章终稿组装成 output/ 里的交付物(不调模型)。返回概况。"""
    import config
    from . import assemble as A
    from .number_gate import _scan_results
    from .outline import OUTLINE_PATH, parse_outline
    from .result_ids import ensure_res_ids
    from .paths import res_catalog_path
    from .results_tables import collect_tables

    out = Path(config.OUTPUT_ROOT)
    out.mkdir(parents=True, exist_ok=True)
    outline = parse_outline(OUTLINE_PATH) if Path(OUTLINE_PATH).is_file() else []
    if not outline:
        raise ValueError(f"没有有效的大纲({OUTLINE_PATH}),先跑 --go 展开并审阅大纲")
    meta = [{"folder": c["folder"], "title": c["title"], "type": c.get("type", ""),
             "number": c["number"]} for c in outline]
    chapters = A.load_chapters(meta, Path(config.WORKSPACE_ROOT))
    tables = {t.name: t for t in collect_tables(config.DATA_ROOT)}
    num = A.number_items(chapters)

    paper_toml = {}
    pt = Path(config.PROJECT_ROOT) / "paper.toml"
    if pt.is_file():
        try:
            paper_toml = tomllib.loads(pt.read_text(encoding="utf-8"))
        except tomllib.TOMLDecodeError:
            paper_toml = {}
    title = str(paper_toml.get("title") or Path(config.PROJECT_ROOT).name)
    authors = [str(a) for a in (paper_toml.get("authors") or []) if isinstance(paper_toml.get("authors"), list)]

    (out / "paper.md").write_text(A.build_paper_md(chapters, num, tables, "en"), encoding="utf-8")
    (out / "paper.zh.md").write_text(A.build_paper_md(chapters, num, tables, "zh"), encoding="utf-8")
    template_dir = Path(config.TEMPLATE_ROOT) if Path(config.TEMPLATE_ROOT).is_dir() else None
    latex = A.build_latex(chapters, num, tables, out / "latex", template_dir, title, authors,
                          Path(config.REFS_BIB_PATH))

    notes = list(extra_notes or [])
    notes += [f"LaTeX 静态检查: {p}" for p in latex["checks"]]
    (out / "待补清单.md").write_text(A.build_todo_report(chapters, notes, num, tables), encoding="utf-8")
    folder_titles = {c.folder: f"{c.number}. {c.title}" for c in chapters}
    (out / "图表清单.md").write_text(A.build_figure_report(num, tables, folder_titles), encoding="utf-8")

    res_map, _ = ensure_res_ids(str(config.DATA_ROOT), res_catalog_path(config.DATA_ROOT))
    _s, _i, tracker = _scan_results(str(config.DATA_ROOT))
    root = str(config.DATA_ROOT)
    sources = {k: v.replace(os.path.join(root, ""), "") for k, v in tracker.sources.items()}
    tables_used = {n: tables[n].file for n in num.tables if n in tables}
    (out / "数字溯源表.md").write_text(A.build_trace_report(chapters, res_map, sources, tables_used),
                                    encoding="utf-8")
    return {"chapters": len(chapters), "with_final": sum(1 for c in chapters if c.en.strip()),
            "missing": [c.folder for c in chapters if not c.en.strip()],
            "unverified": sum(1 for c in chapters if c.status == "unverified"),
            "tables": len(num.tables), "figures": len(num.figures),
            "latex_checks": latex["checks"], "out": str(out)}
