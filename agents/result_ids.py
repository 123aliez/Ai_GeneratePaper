"""RES-ID 占位符体系 —— 数字不经模型之手的结构性防线(移植自 MathModel_Paper 演进点②)。

范式转变:旧防线是"模型自由写数字 + number_gate 事后邻近对账"(概率性查得出);
新防线是"起草期只允许 {{RES-xxx}} 占位符,定稿时程序替换"(结构性写不了)。

data/ 结果库(load_results_store 压平的 {metric: float})按下述规则获得稳定 ID:
- 首次:按 key 字典序分配 RES-001、RES-002…(排序保证同一份 data/ 分配一致);
- 后续:与 data/res-ids.md 中已有映射合并 —— 旧 ID 永不改号(resume 不漂移),
  新增 key 在历史最大编号之后追加;value 一律取当前 data/ 的读数(data/ 改值 →
  ID 不变、值更新,这是"编号是引用、值是事实"的正确语义)。
- key 从 data/ 消失 → 其 ID 转入文件末尾的 "Retired IDs" 列表永久保留,编号
  永不再分配给别的 key(防"同号不同义");同一 key 重新出现时恢复原 ID。

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

from .number_gate import (load_results_store_report, load_results_raw, format_store_issues,
                          ResultsStoreError, _is_number, _try_float)

RES_IDS_FILENAME = "res-ids.md"
RES_ID_RE = re.compile(r"\{\{(RES-\d+)\}\}")
# fill 之后可能出现的未知标记(number gate 同样要剥离,其中的数字不是证据)
UNKNOWN_REF_RE = re.compile(r"⟨未知结果 ([^⟩]+)⟩")
# 派生数字:两个已有结果的差 / 相对提升,由程序计算(可回查来源),模型不手算。
DERIVED_RE = re.compile(r"\{\{(DIFF|REL):\s*(RES-\d+)\s*,\s*(RES-\d+)\s*\}\}")

_HEADER = """\
# res-ids.md — RES-ID catalog (program-generated; do not edit by hand)

Every numeric result under inbox/results/ gets a stable RES-ID. Draft prose cites a
result ONLY as a `{{RES-xxx}}` placeholder copied VERBATIM from the ID column
of this table — never by typing the number. The assembler substitutes the
recorded value at finalization. If you add a unit suffix (%, x, hours, ...),
it must be consistent with the value column.

| RES-ID | results-store key | value |
|---|---|---:|
"""

# ID 列新格式 `{{RES-001}}`(可直接逐字复制),旧格式裸 `RES-001` 仍能解析——
# 否则已有目录会被判损坏、整张表重新编号。
_ROW_RE = re.compile(r"^\|[ \t]*(?:\{\{)?(RES-[0-9]+)(?:\}\})?[ \t]*\|([^|\r\n]+)\|([^|\r\n]*)\|[ \t]*$")
# 退役 ID 列表行。故意不用表格行格式:不匹配 _ROW_RE,Agent 读目录时也不会把
# 退役编号当成可引用的结果。
_RETIRED_HEADING = "## Retired IDs (do not cite)"
_RETIRED_RE = re.compile(r"^- (RES-[0-9]+) \(was: (.+)\)$")


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


def parse_retired_ids(text: str) -> dict:
    """解析退役列表,返回 {res_id: 原 key}。"""
    mapping = {}
    for line in text.splitlines():
        m = _RETIRED_RE.match(line.strip())
        if m:
            mapping[m.group(1)] = m.group(2).strip()
    return mapping


def merge_res_ids_full(store: dict, existing_text: str) -> tuple[dict, dict]:
    """把已有 res-ids.md(活跃表 + 退役列表)合并进新分配。返回 (活跃表, 退役表)。

    历史映射 = 活跃行 ∪ 退役行(同号冲突时活跃行优先):
    - 历史 ID 的 key 仍在 store → 保留该 ID(含退役 key 重新出现 → 恢复原 ID);
    - key 已消失 → 该 ID 进退役表,永久保留,不再分配;
    - 新 key 按字典序从历史最大编号之后追加。
    计数基准是全部历史 ID 而不只是存活的:原先删掉最大编号的 key 后写回的
    目录里不再有这一号,下一次合并就会把它发给新 key。
    """
    numeric = _numeric_store(store)
    history = parse_retired_ids(existing_text)
    history.update(parse_res_ids_md(existing_text))
    active: dict = {}
    retired: dict = {}
    used_keys = set()
    for rid in sorted(history, key=lambda r: int(r.split("-")[1])):
        key = history[rid]
        if key in numeric and key not in used_keys:
            active[rid] = {"key": key, "value": numeric[key]}
            used_keys.add(key)
        else:
            retired[rid] = key
    next_num = 1
    if history:
        next_num = max(int(r.split("-")[1]) for r in history) + 1
    for key in sorted(k for k in numeric if k not in used_keys):
        active[f"RES-{next_num:03d}"] = {"key": key, "value": numeric[key]}
        next_num += 1
    # 按编号排序输出,表格稳定可 diff
    return ({rid: active[rid] for rid in sorted(active)},
            {rid: retired[rid] for rid in sorted(retired)})


def merge_res_ids(store: dict, existing_text: str) -> dict:
    """merge_res_ids_full 的活跃表部分(向后兼容的旧接口)。"""
    return merge_res_ids_full(store, existing_text)[0]


def render_res_ids_md(res_map: dict, retired: dict | None = None) -> str:
    lines = [_HEADER.rstrip("\n")]
    for rid in sorted(res_map):
        entry = res_map[rid]
        # 值列用 repr 级转换(不经 %g 截断六位有效数字),与"never round"纪律一致。
        lines.append(f"| {{{{{rid}}}}} | {entry['key']} | {display_text(entry)} |")
    if retired:
        lines += ["", _RETIRED_HEADING, "",
                  "These IDs belonged to results no longer present in data/. They are",
                  "kept only so the numbers are never reassigned; never cite them.", ""]
        lines += [f"- {rid} (was: {key})" for rid, key in sorted(retired.items())]
    return "\n".join(lines) + "\n"


def display_text(entry: dict) -> str:
    """一条结果替换进正文的文本:优先 CSV 单元格原文(保住 76.10 的小数位),
    没有原文(JSON 来源)时按数值渲染。"""
    text = entry.get("text")
    return text if text else fmt_value(entry["value"])


def _clean_raw(raw, value):
    """CSV 原文 → 可直接替换进正文的文本:去掉尾部 %(单位由正文在花括号外自己加,
    避免出现 76.1%%);原文解析出的数值与库里的值不一致则不用(宁可回退成数值渲染)。"""
    if not raw:
        return None
    text = str(raw).strip()
    if text.endswith("%"):
        text = text[:-1].strip()
    parsed = _try_float(text)
    return text if parsed is not None and parsed == value else None


def fmt_value(value) -> str:
    """把数值渲染成与 Python 字面量一致的文本(整数值不带小数点尾巴)。"""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return repr(value) if isinstance(value, float) else str(value)


def ensure_res_ids(data_root: str, catalog_path=None) -> tuple[dict, Path | None]:
    """生成/合并 RES 编号目录。返回 (res_map, 路径)。

    catalog_path 缺省时写在 data_root/res-ids.md(单元测试用);项目模式下编排器传
    _work/index/res-ids.md,作者的 inbox 保持只读。

    空 store → 返回 ({}, None) 不落盘(data/ 没有数字时硬造一张空表只会误导)。
    结果库有重复 key(不同取值)或读不了的文件 → 抛 ResultsStoreError(调用方硬停)。
    内容未变化时跳过写回(mtime 不抖动)。临时文件带 pid 后缀,避免并发进程
    互踩固定 .tmp 名(os.replace 仍是最后的原子替换)。
    """
    store, issues = load_results_store_report(data_root)
    if issues["duplicates"] or issues["unreadable"]:
        # 结果库有歧义时不生成目录:RES 编号是"引用",同一 key 两个值就不知道
        # 引用的是哪一个;读不了的文件意味着作者以为在库里的数字其实缺席。
        raise ResultsStoreError("data/ 结果库有问题,拒绝生成 RES 目录:\n"
                                + format_store_issues(issues))
    path = Path(catalog_path) if catalog_path else Path(data_root) / RES_IDS_FILENAME
    path.parent.mkdir(parents=True, exist_ok=True)
    if not _numeric_store(store):
        # 数值结果全部消失但目录还在:把历史 ID 全部转入退役列表再返回"无目录",
        # 否则旧 ID 仍以"可引用"的活跃行留在 res-ids.md 里,与 data/ 不一致。
        if path.is_file():
            old_text = path.read_text(encoding="utf-8", errors="replace")
            _active, retired = merge_res_ids_full(store, old_text)
            text = render_res_ids_md({}, retired)
            if text != old_text:
                tmp = path.with_suffix(f".md.{os.getpid()}.tmp")
                tmp.write_text(text, encoding="utf-8")
                os.replace(tmp, path)
        return {}, None
    if path.is_file():
        res_map, retired = merge_res_ids_full(
            store, path.read_text(encoding="utf-8", errors="replace"))
    else:
        res_map, retired = assign_res_ids(store), {}
    raw = load_results_raw(data_root)
    for entry in res_map.values():
        entry["text"] = _clean_raw(raw.get(entry["key"]), entry["value"])
    text = render_res_ids_md(res_map, retired)
    if not (path.is_file() and path.read_text(encoding="utf-8", errors="replace") == text):
        tmp = path.with_suffix(f".md.{os.getpid()}.tmp")
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, path)
    return res_map, path


def _decimals(text: str) -> int:
    """原文的小数位数(含科学计数法:1.00e-3 → 5 位)。"""
    from decimal import Decimal, InvalidOperation
    try:
        exp = Decimal((text or "").replace(",", "").strip()).as_tuple().exponent
    except InvalidOperation:
        return 0
    return max(0, -exp) if isinstance(exp, int) else 0


def _derive(kind: str, a: dict, b: dict) -> str | None:
    """DIFF = a−b(小数位取两者较大者);REL = (a−b)/b×100(1 位小数)。b=0 → None。"""
    va, vb = float(a["value"]), float(b["value"])
    if kind == "DIFF":
        places = max(_decimals(display_text(a)), _decimals(display_text(b)))
        from decimal import Decimal
        try:
            diff = Decimal(display_text(a).replace(",", "")) - Decimal(display_text(b).replace(",", ""))
            return f"{diff:.{places}f}"
        except Exception:
            return f"{va - vb:.{places}f}"
    if vb == 0:
        return None
    return f"{(va - vb) / abs(vb) * 100:.1f}"


def fill_result_refs(text: str, res_map: dict, used: list | None = None) -> tuple[str, list[str]]:
    """把正文中的 {{RES-xxx}} / {{DIFF:..}} / {{REL:..}} 替换为记录值。
    返回 (新文本, 未知项列表)。

    未知 ID(或除数为 0 的相对提升)替换为显式标记 ⟨未知结果 xxx⟩ —— 宁可留下
    一个看得见的洞,也不编一个值填上。used 不为 None 时追加使用记录
    {"placeholder","ids","text","kind"},供数字溯源表使用。
    """
    unresolved: list[str] = []

    def _derived(m: re.Match) -> str:
        kind, ra, rb = m.group(1), m.group(2), m.group(3)
        a, b = res_map.get(ra), res_map.get(rb)
        label = f"{kind}:{ra},{rb}"
        if a is None or b is None:
            unresolved.extend(r for r, e in ((ra, a), (rb, b)) if e is None)
            return f"⟨未知结果 {label}⟩"
        out = _derive(kind, a, b)
        if out is None:
            unresolved.append(label)
            return f"⟨未知结果 {label}⟩"
        if used is not None:
            used.append({"placeholder": m.group(0), "ids": [ra, rb], "text": out, "kind": kind,
                         "negative": out.startswith("-")})
        return out

    def _plain(m: re.Match) -> str:
        rid = m.group(1)
        entry = res_map.get(rid)
        if entry is None:
            unresolved.append(rid)
            return f"⟨未知结果 {rid}⟩"
        out = display_text(entry)
        if used is not None:
            used.append({"placeholder": m.group(0), "ids": [rid], "text": out, "kind": "RES",
                         "negative": False})
        return out

    text = DERIVED_RE.sub(_derived, text)
    return RES_ID_RE.sub(_plain, text), unresolved


# 边界只按 ASCII 字母数字判定:中文紧邻("结果RES-001是")也要抓到。
_BARE_RES_RE = re.compile(r"(?<![A-Za-z0-9_\-])RES-\d+(?![A-Za-z0-9_])")
# 残缺花括号占位符:{RES-001} / {{RES-001} / {RES-001}} ——fill 不会替换它们。
_MALFORMED_RES_RE = re.compile(r"(?<!\{)\{RES-\d+\}?(?!\})|(?<!\{)\{\{RES-\d+\}(?!\})|\{RES-\d+\}\}")


def residual_res_problems(text: str) -> list[str]:
    """终稿里不该再有的 RES 痕迹:未知标记、未替换的占位符、不带花括号的裸编号。

    每次核验都重扫(不依赖"本次是否刚做过替换"):首次定稿把缺口标成
    ⟨未知结果⟩ 后,重跑时填充器看不到 {{RES-}} 了,缺口会被洗成"已核验"。
    """
    problems = []
    unknown = sorted(set(UNKNOWN_REF_RE.findall(text)))
    if unknown:
        problems.append(f"有 {len(unknown)} 个未知结果标记 ⟨未知结果⟩({', '.join(unknown)}):"
                        f"该数字在结果表里不存在,正文留了空洞")
    left_derived = sorted({m.group(0) for m in DERIVED_RE.finditer(text)})
    if left_derived:
        problems.append(f"有 {len(left_derived)} 个未替换的派生数字占位符({', '.join(left_derived)})")
    left = sorted(set(RES_ID_RE.findall(text)))
    if left:
        problems.append(f"有 {len(left)} 个未替换的 {{{{RES-xxx}}}} 占位符({', '.join(left)})")
    scrubbed = DERIVED_RE.sub(" ", RES_ID_RE.sub(" ", UNKNOWN_REF_RE.sub(" ", text)))
    malformed = sorted({m.group(0) for m in _MALFORMED_RES_RE.finditer(scrubbed)})
    if malformed:
        problems.append(f"有 {len(malformed)} 个花括号残缺的占位符({', '.join(malformed)}),"
                        f"不会被替换成数值")
    scrubbed = _MALFORMED_RES_RE.sub(" ", scrubbed)
    bare = sorted(set(_BARE_RES_RE.findall(scrubbed)))
    if bare:
        problems.append(f"有 {len(bare)} 个不带花括号的裸 RES 编号({', '.join(bare)}),"
                        f"不会被替换成数值(若这是正文里的普通词,请人工确认)")
    return problems


def strip_placeholders(text: str) -> str:
    """剥掉占位符与未知标记(number gate 检查前调用)。

    "{{RES-001}}" 中的 "001" 会被数字提取器当成一个数(前导字符 '-' 不在
    字符类里,拦不住),而占位符编号不是实验证据 —— 不剥会造成误报。
    """
    text = DERIVED_RE.sub(" ", text)
    text = RES_ID_RE.sub(" ", text)
    return UNKNOWN_REF_RE.sub(" ", text)
