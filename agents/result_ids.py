"""RES-ID 占位符体系 —— 数字不经模型之手的结构性防线(移植自 MathModel_Paper 演进点②)。

范式转变:旧防线是"模型自由写数字 + number_gate 事后邻近对账"(概率性查得出);
新防线是"起草期只允许 {{RES-xxx}} 占位符,定稿时程序替换"(结构性写不了)。

data/ 结果库(load_results_store 压平的 {metric: float})按下述规则获得稳定 ID:
- 首次:按 key 字典序分配 RES-001、RES-002…(排序保证同一份 data/ 分配一致);
- 后续:与 data/res-ids.md 中已有映射合并 —— 旧 ID 永不改号(resume 不漂移),
  新增 key 在最大编号之后追加;value 一律取当前 data/ 的读数(data/ 改值 →
  ID 不变、值更新,这是"编号是引用、值是事实"的正确语义)。

工作流:
1. orchestrator 在章节路由后调 ensure_res_ids() 生成/合并 data/res-ids.md;
2. 起草提示词(idea_clause 的 DATA/MIXED 分支)指向该文件,纪律:结果数字
   只能逐字复制 {{RES-xxx}},手打结果数字 = 违纪;
3. number_gate 对草稿检查前剥离占位符(只查手打残留,占位符中的 "001"
   不是数字证据);
4. Stage 4 定稿后 fill_result_refs() 替换 final.md / final.zh.md 中的占位符,
   未知 ID 显式标记 ⟨未知结果 xxx⟩ 并记入 todo.md —— 绝不猜值。

标准库实现,无第三方依赖。
"""

import os
import re
from pathlib import Path

from .number_gate import load_results_store, _is_number

RES_IDS_FILENAME = "res-ids.md"
RES_ID_RE = re.compile(r"\{\{(RES-\d+)\}\}")
# fill 之后可能出现的未知标记(number gate 同样要剥离,其中的数字不是证据)
UNKNOWN_REF_RE = re.compile(r"⟨未知结果 (RES-\d+)⟩")

_HEADER = """\
# data/res-ids.md — RES-ID catalog (program-generated; do not edit by hand)

Every numeric result under data/ gets a stable RES-ID. Draft prose cites a
result ONLY as a `{{RES-xxx}}` placeholder copied VERBATIM from the ID column
of this table — never by typing the number. The assembler substitutes the
recorded value at finalization. If you add a unit suffix (%, x, hours, ...),
it must be consistent with the value column.

| RES-ID | results-store key | value |
|---|---|---:|
"""

_ROW_RE = re.compile(r"^\|[ \t]*(RES-[0-9]+)[ \t]*\|([^|\r\n]+)\|([^|\r\n]*)\|[ \t]*$")


def _numeric_store(store: dict) -> dict:
    """只取数值项(文本元数据以 ':' 前缀存储,不占 ID)。"""
    return {k: v for k, v in store.items() if _is_number(v)}


def assign_res_ids(store: dict) -> dict:
    """对数值结果库按 key 字典序分配 RES-001…。

    返回 {res_id: {"key": ..., "value": ...}}。纯函数:同一 store 两次调用
    结果逐字节一致(排序保证),这是 resume 不漂移的基础。
    """
    numeric = _numeric_store(store)
    return {f"RES-{i:03d}": {"key": key, "value": numeric[key]}
            for i, key in enumerate(sorted(numeric), start=1)}


def parse_res_ids_md(text: str) -> dict:
    """从 res-ids.md 文本解析出 {res_id: key}(value 以当前 data/ 为准,不采信旧值)。

    损坏行(半写入/手改/含 | 的 key)不匹配行格式,静默忽略——它们不构成
    "ID 已分配"的证据,缺失的 key 会按新 key 在最大编号后追加,不会复用。
    """
    mapping = {}
    for line in text.splitlines():
        m = _ROW_RE.match(line.strip())
        if m:
            mapping[m.group(1)] = m.group(2).strip()
    return mapping


def merge_res_ids(store: dict, existing_text: str) -> dict:
    """把已有 res-ids.md 的映射合并进新分配。

    旧 ID 对应的 key 仍在 store 里 → 保留旧 ID;key 已消失 → 该 ID 不再分配
    (不复用,防"同号不同义");新 key 按字典序在最大编号后追加。
    计数基准取全部出现过的旧 ID(含已消失的),不只是存活的——否则删掉最大
    编号的 key 后写回目录,下一次合并会把该编号发给新 key。
    """
    numeric = _numeric_store(store)
    old = parse_res_ids_md(existing_text)
    merged: dict = {}
    used_keys = set()
    for rid in sorted(old):
        key = old[rid]
        if key in numeric and key not in used_keys:
            merged[rid] = {"key": key, "value": numeric[key]}
            used_keys.add(key)
    next_num = 1
    if old:
        next_num = max(int(r.split("-")[1]) for r in old) + 1
    for key in sorted(k for k in numeric if k not in used_keys):
        merged[f"RES-{next_num:03d}"] = {"key": key, "value": numeric[key]}
        next_num += 1
    # 按编号排序输出,表格稳定可 diff
    return {rid: merged[rid] for rid in sorted(merged)}


def render_res_ids_md(res_map: dict) -> str:
    lines = [_HEADER.rstrip("\n")]
    for rid in sorted(res_map):
        entry = res_map[rid]
        # 值列用 repr 级转换(不经 %g 截断六位有效数字),与"never round"纪律一致。
        lines.append(f"| {rid} | {entry['key']} | {fmt_value(entry['value'])} |")
    return "\n".join(lines) + "\n"


def fmt_value(value) -> str:
    """把数值渲染成与 Python 字面量一致的文本(整数值不带小数点尾巴)。"""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return repr(value) if isinstance(value, float) else str(value)


def ensure_res_ids(data_root: str) -> tuple[dict, Path | None]:
    """生成/合并 data/res-ids.md。返回 (res_map, 路径)。

    空 store → 返回 ({}, None) 不落盘(data/ 没有数字时硬造一张空表只会误导)。
    内容未变化时跳过写回(mtime 不抖动)。临时文件带 pid 后缀,避免并发进程
    互踩固定 .tmp 名(os.replace 仍是最后的原子替换)。
    """
    store = load_results_store(data_root)
    if not _numeric_store(store):
        return {}, None
    path = Path(data_root) / RES_IDS_FILENAME
    if path.is_file():
        res_map = merge_res_ids(store, path.read_text(encoding="utf-8", errors="replace"))
    else:
        res_map = assign_res_ids(store)
    text = render_res_ids_md(res_map)
    if not (path.is_file() and path.read_text(encoding="utf-8", errors="replace") == text):
        tmp = path.with_suffix(f".md.{os.getpid()}.tmp")
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, path)
    return res_map, path


def fill_result_refs(text: str, res_map: dict) -> tuple[str, list[str]]:
    """把正文中的 {{RES-xxx}} 替换为记录值。返回 (新文本, 未知 ID 列表)。

    未知 ID 替换为显式标记 ⟨未知结果 xxx⟩ —— 宁可留下一个看得见的洞,
    也不编一个值填上。
    """
    unresolved: list[str] = []

    def _repl(m: re.Match) -> str:
        rid = m.group(1)
        entry = res_map.get(rid)
        if entry is None:
            unresolved.append(rid)
            return f"⟨未知结果 {rid}⟩"
        return fmt_value(entry["value"])

    return RES_ID_RE.sub(_repl, text), unresolved


def strip_placeholders(text: str) -> str:
    """剥掉占位符与未知标记(number gate 检查前调用)。

    "{{RES-001}}" 中的 "001" 会被数字提取器当成一个数(前导字符 '-' 不在
    字符类里,拦不住),而占位符编号不是实验证据 —— 不剥会造成误报。
    """
    text = RES_ID_RE.sub(" ", text)
    return UNKNOWN_REF_RE.sub(" ", text)
