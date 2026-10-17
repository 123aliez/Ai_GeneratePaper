"""结果表:数据索引与表格渲染(纯标准库,不调模型)。

inbox/results/ 里每个 CSV 是论文里的一张表:第一列是方法/配置名,其余列是指标;
列名结尾的 ↑/↓ 表示越大/越小越好;`X_std`(或 X_stderr / X_se)列是指标 X 的标准差。

本模块用 number_gate._csv_cells 枚举单元格,所以这里的 key 与 RES 目录、数字门禁
永远一致。输出两样东西:
- 数据索引 data-index.md:每张表一个网格,格子里是 `{{RES-xxx}}=原文`,Agent 靠它
  找编号;程序生成,数据一变就重建(不再由模型抄写);
- 表格渲染:正文里写 `[TABLE: 表名 | caption: ... | note: ...]`,组装时由程序按 CSV
  渲染成 Markdown / LaTeX 表(数字不经模型之手)。
"""

from __future__ import annotations

import csv
import os
import re
from dataclasses import dataclass, field

from .number_gate import _csv_cells, results_prefix, is_example_file

_STD_SUFFIX_RE = re.compile(r"^(?P<base>.+?)[\s_\-]*(?:std|stderr|stddev|se|sem)$", re.IGNORECASE)
_MAX_INDEX_ROWS = 40          # 数据索引里每张表最多列出的行数(多出的写明省略)


@dataclass
class Cell:
    key: str
    value: float
    raw: str


@dataclass
class Table:
    name: str                       # 表名 = key 前缀(通常是 CSV 文件名去扩展名)
    file: str                       # 相对结果目录的路径
    label_header: str = ""          # 第一列表头(方法 / Method)
    cols: list = field(default_factory=list)      # 列名(原样,含 ↑/↓)
    labels: list = field(default_factory=list)    # 行名(按文件顺序)
    cells: dict = field(default_factory=dict)     # (label, col) → Cell
    flat: list = field(default_factory=list)      # 没有行名的数值(长表/单行)→ [(key, value, raw)]
    collided: bool = False                        # 同一 (行名,列名) 出现多个值(如多 seed 未聚合)
    text_cells: dict = field(default_factory=dict)   # (行名, 列名) → 非数值单元格原文(76.1† / N/A / OOM)
    col_order: list = field(default_factory=list)    # 表头里的列序

    @property
    def is_grid(self) -> bool:
        return bool(self.labels and self.cols) and not self.collided


def strip_arrow(col: str) -> tuple[str, str]:
    """列名 → (显示名, 方向)。方向: '↑' / '↓' / ''。"""
    c = col.strip()
    if c.endswith("↑"):
        return c[:-1].strip(), "↑"
    if c.endswith("↓"):
        return c[:-1].strip(), "↓"
    return c, ""


def pair_std_columns(cols: list) -> tuple[list, dict]:
    """找出 (指标列, 标准差列) 配对。返回 (要显示的列, {指标列: 标准差列})。
    标准差列的基名与某个指标列的显示名一致(忽略 ↑/↓ 与大小写)才算配对。"""
    base_by_name = {strip_arrow(c)[0].lower(): c for c in cols}
    pairs: dict = {}
    for c in cols:
        m = _STD_SUFFIX_RE.match(strip_arrow(c)[0])
        if not m:
            continue
        target = base_by_name.get(m.group("base").strip().lower())
        if target and target != c:
            pairs[target] = c
    hidden = set(pairs.values())
    return [c for c in cols if c not in hidden], pairs


def collect_tables(results_root) -> list[Table]:
    """按文件顺序收集所有 CSV 表。示例文件(.example.)跳过,与结果库一致。"""
    root = str(results_root)
    tables: dict = {}
    order: list = []
    if not os.path.isdir(root):
        return []
    for dirpath, _dirs, filenames in sorted(os.walk(root)):
        for fn in sorted(filenames):
            if is_example_file(fn) or os.path.splitext(fn)[1].lower() != ".csv":
                continue
            path = os.path.join(dirpath, fn)
            name = results_prefix(path)
            table = tables.get(name)
            if table is None:
                table = Table(name=name, file=os.path.relpath(path, root))
                tables[name] = table
                order.append(name)
            try:
                with open(path, "r", newline="", encoding="utf-8-sig", errors="replace") as f:
                    first = next(csv.reader(f), [])
                if first and not table.label_header:
                    table.label_header = first[0].strip()
                for cell in _csv_cells(path, name):
                    c = Cell(cell["key"], cell["value"], cell["raw"])
                    if cell["label"]:
                        if (cell["label"], cell["col"]) in table.cells:
                            table.collided = True        # 不静默取最后一次:整表退化为逐项引用
                        if cell["label"] not in table.labels:
                            table.labels.append(cell["label"])
                        if cell["col"] not in table.cols:
                            table.cols.append(cell["col"])
                        table.cells[(cell["label"], cell["col"])] = c
                        table.flat.append((c.key, c.value, c.raw))
                    else:
                        table.flat.append((c.key, c.value, c.raw))
                _collect_text_cells(path, table)
            except Exception:
                continue            # 读不了的文件由结果库的 unreadable 报告,这里不重复
    return [tables[n] for n in order]


def _collect_text_cells(path: str, table: Table) -> None:
    """宽表里的非数值单元格(`76.1†`、`N/A`、`OOM`)原样保留显示,不编 RES 编号;
    列序按表头顺序(不是各列首次出现数值的顺序)。"""
    with open(path, "r", newline="", encoding="utf-8-sig", errors="replace") as f:
        rows = [r for r in csv.reader(f) if r]
    if len(rows) < 2 or not table.labels:
        return
    header = [h.strip() for h in rows[0]]
    id_cols = {i for i, h in enumerate(header) if h.lower() in ("seed", "run", "run_id", "trial", "fold")}
    if id_cols or any(h.lower() in ("name", "metric", "key", "metric_name") for h in header):
        return
    for r in rows[1:]:
        label = r[0].strip() if r else ""
        if label not in table.labels:
            continue
        for ci in range(1, len(r)):
            col = header[ci] if ci < len(header) and header[ci] else f"col{ci}"
            raw = r[ci].strip()
            if raw and (label, col) not in table.cells:
                table.text_cells[(label, col)] = raw
                if col not in table.cols:
                    table.cols.append(col)
    for col in header[1:]:
        if col and col in table.cols and col not in table.col_order:
            table.col_order.append(col)
    if table.col_order:
        table.cols = [c for c in table.col_order] + [c for c in table.cols if c not in table.col_order]


def table_names(results_root) -> list[str]:
    return [t.name for t in collect_tables(results_root) if t.is_grid]


# ── 数据索引 ─────────────────────────────────────────────────────────────
def render_data_index(tables: list, res_map: dict, settings_path: str = "") -> str:
    """数据索引(程序生成)。res_map: ensure_res_ids 返回的 {RES-xxx: {key, value}}。"""
    rid_by_key = {e["key"]: rid for rid, e in res_map.items()}
    lines = [
        "# 数据索引(程序生成,勿手改;源头是 inbox/results/)",
        "",
        "正文里的结果数字只能写成 `{{RES-xxx}}` 占位符(逐字复制下表格子里的编号,单位写在花括号外),",
        "两个结果的差/相对提升写 `{{DIFF:RES-a,RES-b}}`(a−b)/`{{REL:RES-a,RES-b}}`((a−b)/b×100,单位 % 写在花括号外)。",
        "结果表整张放进正文用 `[TABLE: 表名 | caption: 表题 | note: 表注]`,表里的数字由程序按 CSV 生成,不要自己写。",
    ]
    if settings_path:
        lines.append(f"实验设置(数据集/硬件/超参/seed 数)在 `{settings_path}`,实验类章节整份读取。")
    lines.append("")
    if not tables:
        lines.append("(inbox/results/ 里没有结果表)")
        return "\n".join(lines) + "\n"
    for t in tables:
        if t.is_grid:
            shown_cols, pairs = pair_std_columns(t.cols)
            lines.append(f"## {t.name}  (`{t.file}` · {len(t.labels)} 行 × {len(shown_cols)} 列)")
            lines.append("")
            lines.append("列方向: " + "; ".join(
                f"{strip_arrow(c)[0]}{strip_arrow(c)[1] or '(无方向)'}" for c in shown_cols)
                + ("。X_std 列是 X 的标准差,表格里自动拼成 ‘均值 ± 标准差’。" if pairs else ""))
            lines.append("")
            head = [t.label_header or "名称"] + [c for c in t.cols]
            lines.append("| " + " | ".join(head) + " |")
            lines.append("|" + "|".join(["---"] * len(head)) + "|")
            for label in t.labels[:_MAX_INDEX_ROWS]:
                row = [label]
                for c in t.cols:
                    cell = t.cells.get((label, c))
                    if cell is None:
                        row.append(t.text_cells.get((label, c), ""))
                    else:
                        rid = rid_by_key.get(cell.key, "?")
                        row.append(f"{{{{{rid}}}}}={cell.raw}")
                lines.append("| " + " | ".join(row) + " |")
            if len(t.labels) > _MAX_INDEX_ROWS:
                lines.append(f"\n(另有 {len(t.labels) - _MAX_INDEX_ROWS} 行省略;完整编号见 res-ids.md)")
            lines.append("")
        else:
            lines.append(f"## {t.name}  (`{t.file}` · 无行名的数值)")
            lines.append("")
            for key, _value, raw in t.flat[:_MAX_INDEX_ROWS]:
                rid = rid_by_key.get(key, "?")
                lines.append(f"- {key}: {{{{{rid}}}}}={raw}")
            lines.append("")
    return "\n".join(lines) + "\n"


# ── 表格渲染 ─────────────────────────────────────────────────────────────
def _best_labels(table: Table, col: str, direction: str) -> set:
    if not direction:
        return set()
    vals = [(l, table.cells[(l, col)].value) for l in table.labels if (l, col) in table.cells]
    if not vals:
        return set()
    best = (max if direction == "↑" else min)(v for _, v in vals)
    return {l for l, v in vals if v == best}


def table_matrix(table: Table) -> tuple[list, list, list]:
    """渲染用的中间表示:(表头显示名, 对齐方向, 行) 。行是 [(是否加粗, 文本)] 列表。

    均值/标准差列合并为 `均值 ± 标准差`(原文拼接,不重新格式化);每列按 ↑/↓ 加粗最优值。
    """
    shown, pairs = pair_std_columns(table.cols)
    header = [table.label_header or "Method"]
    for c in shown:
        name, arrow = strip_arrow(c)
        header.append(f"{name} {arrow}".strip())
    rows = []
    best = {c: _best_labels(table, c, strip_arrow(c)[1]) for c in shown}
    for label in table.labels:
        row = [(False, label)]
        for c in shown:
            cell = table.cells.get((label, c))
            if cell is None:
                row.append((False, table.text_cells.get((label, c), "—")))
                continue
            text = cell.raw
            std = table.cells.get((label, pairs[c])) if c in pairs else None
            if std is not None:
                text = f"{text} ± {std.raw}"
            row.append((label in best[c], text))
        rows.append(row)
    return header, rows


def render_table_markdown(table: Table, caption: str = "", note: str = "", number: int = 0) -> str:
    header, rows = table_matrix(table)
    out = []
    if caption:
        out.append(f"**Table {number or ''}. {caption}**".replace("Table . ", "Table. "))
        out.append("")
    out.append("| " + " | ".join(header) + " |")
    out.append("|" + "|".join(["---"] + ["---:"] * (len(header) - 1)) + "|")
    for row in rows:
        out.append("| " + " | ".join(f"**{t}**" if bold else t for bold, t in row) + " |")
    if note:
        out.append("")
        out.append(f"*{note}*")
    return "\n".join(out)


_TEX_SPECIALS = {"&": r"\&", "%": r"\%", "_": r"\_", "#": r"\#", "$": r"\$", "{": r"\{",
                 "}": r"\}", "~": r"\textasciitilde{}", "^": r"\textasciicircum{}"}


def tex_escape(text: str) -> str:
    text = text.replace("\\", r"\textbackslash{}")
    text = "".join(_TEX_SPECIALS.get(ch, ch) for ch in text)
    return text.replace("↑", r"$\uparrow$").replace("↓", r"$\downarrow$").replace("±", r"$\pm$")


def render_table_latex(table: Table, caption: str = "", note: str = "", label: str = "") -> str:
    header, rows = table_matrix(table)
    colspec = "l" + "r" * (len(header) - 1)
    out = [r"\begin{table}[t]", r"\centering"]
    if caption:
        out.append(rf"\caption{{{tex_escape(caption)}}}")
    if label:
        out.append(rf"\label{{{label}}}")
    out += [rf"\begin{{tabular}}{{{colspec}}}", r"\toprule",
            " & ".join(tex_escape(h) for h in header) + r" \\", r"\midrule"]
    for row in rows:
        cells = [rf"\textbf{{{tex_escape(t)}}}" if bold else tex_escape(t) for bold, t in row]
        out.append(" & ".join(cells) + r" \\")
    out += [r"\bottomrule", r"\end{tabular}"]
    if note:
        out.append(rf"\par\vspace{{2pt}}{{\footnotesize {tex_escape(note)}}}")
    out.append(r"\end{table}")
    return "\n".join(out)


# ── 占位符解析 ───────────────────────────────────────────────────────────
TABLE_PLACEHOLDER_RE = re.compile(r"^[ \t]*\[TABLE:\s*(?P<body>[^\]\n]+)\][ \t]*$", re.MULTILINE)


def parse_table_placeholder(body: str) -> dict:
    """`表名 | caption: ... | note: ...` → {name, caption, note}。"""
    parts = [p.strip() for p in body.split("|")]
    out = {"name": parts[0] if parts else "", "caption": "", "note": ""}
    for part in parts[1:]:
        m = re.match(r"(caption|note|label)\s*[:：]\s*(.*)$", part, re.IGNORECASE | re.DOTALL)
        if m:
            out[m.group(1).lower()] = m.group(2).strip()
    return out


def find_table_placeholders(text: str) -> list[dict]:
    return [parse_table_placeholder(m.group("body")) for m in TABLE_PLACEHOLDER_RE.finditer(text or "")]


def unknown_tables(text: str, known: set) -> list[str]:
    return sorted({p["name"] for p in find_table_placeholders(text) if p["name"] not in known})
