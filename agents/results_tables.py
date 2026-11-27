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

_STD_SUFFIX_RE = re.compile(r"^(?:(?P<base>.+?)[\s_\-]+)?(?:std|sd|stderr|stddev|se|sem|standard[ _-]+(?:deviation|error)|标准差|标准误)$", re.IGNORECASE)
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
    dispersion_keys: set = field(default_factory=set)  # 含行号的扁平键不能按末段猜列名
    col_order: list = field(default_factory=list)    # 表头里的列序
    reference_only: bool = False                   # ref_ CSV 只供正文取数

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


def _is_std_column(col: str) -> bool:
    """判断列名是否为标准差/标准误列(如 Acc_std, F1-se)。"""
    return bool(_STD_SUFFIX_RE.match(strip_arrow(col)[0]))


def is_sd_column(col: str) -> bool:
    """作图只取标准差，区分同样在正文隐藏的标准误。"""
    match = _STD_SUFFIX_RE.fullmatch(strip_arrow(col)[0])
    return bool(match and re.search(r"(?:std|stddev|sd|standard[ _-]+deviation|标准差)$",
                                    match[0], re.I))


def pair_std_columns(cols: list) -> tuple[list, dict]:
    """找出 (指标列, 标准差列) 配对。返回 (不含标准差列的显示列, {指标列: 标准差列})。
    所有匹配标准差命名规则的列均被隐藏(即使未找到对应均值列), 防止泄漏进数据索引与正文表。"""
    base_by_name = {strip_arrow(c)[0].lower(): c for c in cols if not _is_std_column(c)}
    pairs: dict = {}
    hidden: set = set()
    for c in cols:
        m = _STD_SUFFIX_RE.match(strip_arrow(c)[0])
        if not m:
            continue
        hidden.add(c)
        target = base_by_name.get((m.group("base") or "").strip().lower())
        if target and target != c:
            pairs[target] = c
    return [c for c in cols if c not in hidden], pairs


def _filter_shown_cols(shown: list[str], selected: list[str] | None) -> list[str]:
    """按占位符指定的 cols: 过滤显示列(支持完整列名或去 ↑/↓ 基名, 忽略大小写)。"""
    if selected is None:
        return shown
    by_exact = {c.strip().lower(): c for c in shown}
    by_base = {strip_arrow(c)[0].lower(): c for c in shown}
    filtered: list[str] = []
    for item in selected:
        key = item.strip().lower()
        base_key = strip_arrow(item)[0].lower()
        match = by_exact.get(key) or by_base.get(base_key)
        if not match:
            raise ValueError(f"无法选取列 {item!r}，可用列为 {', '.join(shown)}")
        if match not in filtered:
            filtered.append(match)
    if not filtered:
        raise ValueError("cols 需要至少一个正文指标列")
    return filtered


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
                table = Table(name=name, file=os.path.relpath(path, root), reference_only=fn.lower().startswith("ref_"))
                tables[name] = table
                order.append(name)
            try:
                with open(path, "r", newline="", encoding="utf-8-sig", errors="replace") as f:
                    first = next(csv.reader(f), [])
                if first and not table.label_header:
                    table.label_header = first[0].strip()
                for cell in _csv_cells(path, name):
                    c = Cell(cell["key"], cell["value"], cell["raw"])
                    if _is_std_column(cell["col"]):
                        table.dispersion_keys.add(c.key)
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


_MEAN_SPREAD_CELL = re.compile(
    r"(?P<mean>[+-]?[\d,.]+(?:[eE][+-]?\d+)?%?)\s*(?:±|\+/-)\s*"
    r"[\d,.]+(?:[eE][+-]?\d+)?(?P<unit>%?)(?P<mark>[†‡*]*)"
)


def _main_cell(raw: str) -> str:
    """部分旧 CSV 把均值与波动写在同一格，正文显示时保留单位与脚注。"""
    match = _MEAN_SPREAD_CELL.fullmatch(raw)
    if not match:
        return raw
    mean = match["mean"]
    return mean + (match["unit"] if not mean.endswith("%") else "") + match["mark"]


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
    return [t.name for t in collect_tables(results_root) if t.is_grid and not t.reference_only]


# ── 数据索引 ─────────────────────────────────────────────────────────────
def render_data_index(tables: list, res_map: dict, settings_path: str = "") -> str:
    """数据索引(程序生成)。res_map: ensure_res_ids 返回的 {RES-xxx: {key, value}}。"""
    rid_by_key = {e["key"]: rid for rid, e in res_map.items()}
    lines = [
        "# 数据索引(程序生成,勿手改;源头是 inbox/results/)",
        "",
        "正文里的结果数字只能写成 `{{RES-xxx}}` 占位符(逐字复制下表格子里的编号,单位写在花括号外),",
        "两个结果的差/相对提升写 `{{DIFF:RES-a,RES-b}}`(a−b)/`{{REL:RES-a,RES-b}}`((a−b)/b×100,单位 % 写在花括号外)。",
        "结果表整张放进正文用 `[TABLE: 表名 | cols: 核心列1, 核心列2 | caption: 表题 | note: 极简表注]`,",
        "表里的数字由程序按 CSV 生成,不要自己写。表格与正文仅报告均值/主指标。",
    ]
    if settings_path:
        lines.append(f"实验设置(数据集/硬件/超参/seed 数)在 `{settings_path}`,实验类章节仅提取高层次核心协议。")
    lines.append("")
    if not tables:
        lines.append("(inbox/results/ 里没有结果表)")
        return "\n".join(lines) + "\n"
    for t in tables:
        if t.reference_only:
            lines.append(f"> {t.name}: 仅供引用数字，禁止使用 [TABLE: {t.name}] 插表。")
        if t.is_grid:
            shown_cols, _pairs = pair_std_columns(t.cols)
            lines.append(f"## {t.name}  (`{t.file}` · {len(t.labels)} 行 × {len(shown_cols)} 列)")
            lines.append("")
            lines.append("列方向: " + "; ".join(
                f"{strip_arrow(c)[0]}{strip_arrow(c)[1] or '(无方向)'}" for c in shown_cols))
            lines.append("")
            head = [t.label_header or "名称"] + list(shown_cols)
            lines.append("| " + " | ".join(head) + " |")
            lines.append("|" + "|".join(["---"] * len(head)) + "|")
            for label in t.labels[:_MAX_INDEX_ROWS]:
                row = [label]
                for c in shown_cols:
                    cell = t.cells.get((label, c))
                    if cell is None:
                        row.append(_main_cell(t.text_cells.get((label, c), "")))
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
                if key in t.dispersion_keys:
                    continue
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


def table_matrix(
    table: Table,
    cols: list[str] | None = None,
    include_std: bool = False,
) -> tuple[list, list]:
    """渲染用的中间表示:(表头显示名, 行)。行是 [(是否加粗, 文本)] 列表。
    默认 include_std=False: 不拼接标准差(± std), 仅渲染均值/主指标; 每列按 ↑/↓ 加粗最优值。"""
    if table.reference_only:
        raise ValueError(f"{table.name} 是仅供引用数字的数据，禁止使用 [TABLE] 插表")
    shown, pairs = pair_std_columns(table.cols)
    shown = _filter_shown_cols(shown, cols)
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
                raw = table.text_cells.get((label, c), "—")
                row.append((False, raw if include_std else _main_cell(raw)))
                continue
            text = cell.raw
            if include_std and c in pairs:
                std = table.cells.get((label, pairs[c]))
                if std is not None:
                    text = f"{text} ± {std.raw}"
            row.append((label in best[c], text))
        rows.append(row)
    return header, rows


def render_table_markdown(
    table: Table,
    caption: str = "",
    note: str = "",
    number: int = 0,
    cols: list[str] | None = None,
    include_std: bool = False,
    panels: bool = False,
) -> str:
    header, rows = table_matrix(table, cols=cols, include_std=include_std)
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
    return text.replace("σ", r"$\sigma$").replace("↑", r"$\uparrow$").replace("↓", r"$\downarrow$").replace("±", r"$\pm$")


# 表注/表题里模型写的 \cite{key}:原样保留成真正的引用。其余一律转义(防模型文本夹带命令)。
# 真实测试里 `note: Results reported by Hu et al. \cite{hu2018squeeze}` 被整体转义成了
# 字面文字 "\textbackslash\{\}cite\{...\}",编译后是乱码、参考文献也不被引用。
_KEEP_CITE_RE = re.compile(r"\\cite[pt]?\{[A-Za-z0-9_:.+\-/]+(?:\s*,\s*[A-Za-z0-9_:.+\-/]+)*\}")


def tex_escape_keep_cite(text: str) -> str:
    pieces, last = [], 0
    for m in _KEEP_CITE_RE.finditer(text or ""):
        pieces.append(tex_escape(text[last:m.start()]))
        pieces.append(re.sub(r"\s+", "", m.group(0)))
        last = m.end()
    pieces.append(tex_escape((text or "")[last:]))
    return "".join(pieces)


def render_table_latex(
    table: Table,
    caption: str = "",
    note: str = "",
    label: str = "",
    cols: list[str] | None = None,
    include_std: bool = False,
    panels: bool = False,
) -> str:
    header, rows = table_matrix(table, cols=cols, include_std=include_std)
    label_spec = "l"
    long_labels = any(len(row[0][1]) > 28 for row in rows)
    if long_labels:
        label_spec = r">{\raggedright\arraybackslash}p{0.28\linewidth}"
    env = "table*" if len(header) >= 5 or long_labels else "table"
    out = [rf"\begin{{{env}}}[!t]", r"\centering"]
    if caption or label:
        out.append(rf"\caption{{{tex_escape_keep_cite(caption)}}}")
    if label:
        out.append(rf"\label{{{label}}}")
    colspec = label_spec + "r" * (len(header) - 1)
    # 同一数据集下的指标共享上层表头，降低宽表为了容纳重复文字而缩小的比例。
    labels = [h.partition(" ") for h in header[1:]]
    groups = []
    for dataset, _, metric in labels:
        if groups and groups[-1][0] == dataset:
            groups[-1][1] += 1
        else:
            groups.append([dataset, 1])
    header_lines = []
    if len(groups) >= 2 and all(n >= 2 for _, n in groups) and all(m.strip(" ↑↓") for _, _, m in labels):
        header_lines.append(" & " + " & ".join(
            rf"\multicolumn{{{n}}}{{c}}{{{tex_escape(dataset)}}}" for dataset, n in groups) + r" \\")
        header = [header[0], *(metric for _, _, metric in labels)]
    header_lines.append(" & ".join(tex_escape(h) for h in header) + r" \\")
    out += [r"\setbox0=\hbox{", rf"\begin{{tabular}}{{{colspec}}}", r"\toprule",
            *header_lines, r"\midrule"]
    for row in rows:
        cells = [rf"\textbf{{{tex_escape(t)}}}" if bold else tex_escape(t) for bold, t in row]
        out.append(" & ".join(cells) + r" \\")
    out += [r"\bottomrule", r"\end{tabular}}",
            r"\noindent\makebox[\linewidth][c]{\ifdim\wd0>\linewidth\resizebox{\linewidth}{!}{\box0}\else\box0\fi}\par"]
    if note:
        out.append(rf"\par\vspace{{2pt}}{{\footnotesize {tex_escape_keep_cite(note)}}}")
    out.append(rf"\end{{{env}}}")
    return "\n".join(out)


# ── 占位符解析 ───────────────────────────────────────────────────────────
TABLE_PLACEHOLDER_RE = re.compile(r"^[ \t]*\[TABLE:\s*(?P<body>[^\]\n]+)\][ \t]*$", re.MULTILINE)


def parse_table_placeholder(body: str) -> dict:
    """`表名 | cols: 列1, 列2 | caption: ... | note: ...` → {name, cols, caption, note, label}。"""
    parts = [p.strip() for p in body.split("|")]
    out: dict = {"name": parts[0] if parts else "", "caption": "", "note": ""}
    for part in parts[1:]:
        m = re.match(r"(caption|note|label|cols|layout)\s*[:：]\s*(.*)$", part, re.IGNORECASE | re.DOTALL)
        if not m:
            continue
        key, val = m.group(1).lower(), m.group(2).strip()
        if key == "cols":
            out["cols"] = [c.strip() for c in re.split(r"[,，;；]", val) if c.strip()]
        else:
            out[key] = val
    return out


def find_table_placeholders(text: str) -> list[dict]:
    return [parse_table_placeholder(m.group("body")) for m in TABLE_PLACEHOLDER_RE.finditer(text or "")]


_LAYOUT_GAP = re.compile(r"^\s*\[UNKNOWN:\s*Renderer-supported layout for a single "
                         r"([^\s|\]]+) insertion\b[^\]\n]*\][ \t]*$", re.I | re.M)


def recover_table_layouts(text: str, tables: dict) -> str:
    """旧稿中明确指向可渲染 CSV 的排版占位可转成标准插表指令。"""
    def replace(match):
        name = match[1]
        table = tables.get(name)
        if not table or not table.is_grid or table.reference_only:
            return match[0]
        cols = pair_std_columns(table.cols)[0]
        metric_list = re.search(r"retaining (.+?) for each dataset", match[0], re.I)
        if metric_list:
            metrics = re.split(r",\s*|\s+and\s+", metric_list[1])
            selected = [c for c in cols if any(strip_arrow(c)[0].lower().endswith(" " + m.lower())
                                               for m in metrics)]
            if not selected or any(not any(strip_arrow(c)[0].lower().endswith(" " + m.lower())
                                           for c in selected) for m in metrics):
                return match[0]
            cols = selected
        selection = f" | cols: {', '.join(cols)}" if metric_list else ""
        return f"\n[TABLE: {name}{selection}]\n"
    return _LAYOUT_GAP.sub(replace, text)


def unknown_tables(text: str, known: set) -> list[str]:
    return sorted({p["name"] for p in find_table_placeholders(text) if p["name"] not in known})


_RESULT_SPREAD_RE = re.compile(
    r"(?:\d|\}\})\s*\$?\s*(?:±|\\pm\b|\+/-)\s*\$?\s*(?:\d|\{\{)|"
    r"\b(?:standard (?:deviations?|errors?)|std(?:dev)?|stderr)\b|标准差|标准误",
    re.I,
)


def table_style_warnings(text: str, tables_by_name: dict, *, result_prose: bool = True, english: bool = True) -> list[str]:
    """检查正文表的实际选列、表注及结果表述。数学符号本身不是统计量。"""
    warnings = []
    for match in TABLE_PLACEHOLDER_RE.finditer(text):
        p = parse_table_placeholder(match["body"])
        caption = p["caption"]
        words = re.findall(r"[A-Za-z]+(?:[-'][A-Za-z]+)*", caption)
        stem = re.sub(r"[\W_]+", "", p["name"].removesuffix(".csv")).casefold()
        if (not caption or (english and len(words) < 3)
                or re.sub(r"[\W_]+", "", caption.removesuffix(".csv")).casefold() == stem):
            warnings.append(f"Table {p['name']}: supply a descriptive caption (English: at least three words).")
        after = text[match.end():].lstrip()
        paragraph = re.split(r"\n\s*\n", after, maxsplit=1)[0]
        if result_prose and (not paragraph or re.match(r"(?:[#|>]|[-*]\s|\[(?!\[TAB:)|\$\$)", paragraph)):
            warnings.append(f"Table {p['name']}: follow the table immediately with a prose paragraph interpreting its result.")
        if len(p["note"].split()) > 30:
            warnings.append(f"Table {p['name']}: note exceeds 30 words.")
        table = tables_by_name.get(p["name"])
        if table and table.reference_only:
            warnings.append(f"Table {p['name']}: reference-only data cannot be inserted as a manuscript table.")
        if table and table.is_grid:
            try:
                shown = _filter_shown_cols(pair_std_columns(table.cols)[0], p.get("cols"))
            except ValueError as exc:
                warnings.append(f"Table {p['name']}: {exc}")
            else:
                if len(shown) > 6 and (not p.get("cols") or len(shown) == len(pair_std_columns(table.cols)[0]) and len(shown) > 9):
                    warnings.append(f"Table {p['name']}: {len(shown)} columns. Select core metrics with cols:.")
    for line in text.splitlines():
        # 标识符与来源只用于程序定位，检查的是读者看到的文字。
        if line.startswith(("Data:", "Source:", "Supports:", "Type:")):
            continue
        if line.lstrip().startswith("[TABLE:"):
            fields = find_table_placeholders(line)
            if fields:
                line = fields[0]["caption"] + " " + fields[0]["note"]
                check = True
            else:
                check = result_prose
        else:
            check = result_prose or line.lstrip().startswith(("Note.", "[FIGURE", "|"))
        if check and _RESULT_SPREAD_RE.search(line):
            warnings.append(f"Check experimental dispersion in manuscript: {line.strip()[:140]}")
    return warnings
