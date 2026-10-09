"""inbox 散装材料前处理层 —— `python run.py --intake`（纯 Python，零模型调用）。

输入形式向 MathModel_Paper 的 inbox 靠拢：作者把 idea 文档 / 代码 / 数据 /
文献等乱扔进 inbox/，本模块扫描盘点后转成框架自己的三份事实源契约：

- idea.md    ：找到散件里已写好的 idea 文档，**逐字复制**转正（不生成不提炼）；
- outline    ：不在本层职责内 —— 作者仍写 outline_draft.md → --expand 老路；
- data/      ：只对账（哪些文件会被 load_results_store 读入），不搬运不转换。

设计要点（与 MathModel intake 的对应关系）：
- 角色初判 classify()：目录信号 > 文件名正则 > 扩展名兜底（inventory.py 简化版，
  本框架无 template/planner 通道，dataset 与 results 统一归 data）；
- sha256 快照 + snapshot_id：材料没变 → 重跑 --intake 幂等跳过（snapshot.py 同构）；
- 报告与 manifest 落在 var/intake/（inbox 之外——写进 inbox 会改变其哈希，
  幂等跳过就永不成立）；
- 多 idea 候选不猜：列出让作者用 `--intake --idea <path>` 绑定（idea.md 被每个
  Agent 每阶段全文读，选错污染全篇；MathModel project.yaml final_model 同一先例）；
- data 对账复用 number_gate 的 loader 内部件：报告说"读得进"必须与真 loader
  逐字节同判据，不能自造一套平行逻辑然后两边漂移。

路径解析约定：所有公开函数接受显式参数，默认 None 时**在调用时**经
``config.XXX`` 属性访问解析（不用 from-import —— 那会冻结模块级副本，
测试 patch 不生效；见 tests/test_pipeline_routing.py 的 set_idea() 惯例）。
"""

import hashlib
import json
import os
import re
import shutil
import sys
from datetime import datetime
from pathlib import Path

from .number_gate import _GENERIC_STEMS, _flatten, _load_csv

# 角色集合（ROLES 仅作盘点分类用；流水线不消费这些角色）。
ROLES = ("idea", "problem", "data", "code", "reference", "figure", "unknown")

# 文档扩展名里能直接当 idea 转正的（load_idea_document 按文本读得出）。
_TEXT_IDEA_EXTS = {".md", ".txt"}
# 只列不转正的二进制文档（纯标准库不解析 pdf/docx；引入解析器不在本项目依赖面）。
_BINARY_DOC_EXTS = {".pdf", ".docx"}

_DIR_ROLE_PATTERNS = [
    (re.compile(r"^(reference|references|文献)$", re.I), "reference"),
    # 本框架不分 dataset/results：数据类目录统一归 data（数字防线只看 load_results_store）。
    (re.compile(r"^(data|dataset|数据|results|实验结果)$", re.I), "data"),
    (re.compile(r"^(code|src|代码)$", re.I), "code"),
]
_DOC_NAME_PATTERNS = [
    (re.compile(r"(idea|模型说明|模型|方法|method|思路|方案|创新)", re.I), "idea"),
    (re.compile(r"(赛题|题目|problem)", re.I), "problem"),
]
_CODE_EXTS = {".py", ".ipynb", ".m", ".r", ".c", ".cpp", ".h", ".hpp", ".sh", ".jl"}
_DATA_EXTS = {".json", ".csv", ".tsv", ".xlsx", ".xls", ".mat"}
_FIGURE_EXTS = {".png", ".jpg", ".jpeg", ".tiff", ".svg"}


def classify(rel_path: str) -> str:
    """单个文件的角色初判。目录信号 > 文件名正则 > 扩展名兜底。

    判不出 → unknown（宁可如实说不知道，也不猜一个角色误导盘点报告）。
    """
    parts = [p for p in Path(rel_path).parts[:-1]]
    # 1) 目录信号：任一路径段命中即定角色（code/ 里任何文件都算 code 材料）。
    for pattern, role in _DIR_ROLE_PATTERNS:
        if any(pattern.match(seg) for seg in parts):
            return role
    ext = Path(rel_path).suffix.lower()
    stem = Path(rel_path).stem
    # 2) 文件名信号（仅文档扩展名参与 idea/problem 判定，防 xlsx 撞上"方案"之类）。
    if ext in _TEXT_IDEA_EXTS or ext in _BINARY_DOC_EXTS:
        for pattern, role in _DOC_NAME_PATTERNS:
            if pattern.search(stem):
                return role
    # 3) 扩展名兜底。
    if ext in _CODE_EXTS:
        return "code"
    if ext in _DATA_EXTS:
        return "data"
    if ext in _FIGURE_EXTS:
        return "figure"
    return "unknown"


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            digest.update(chunk)
    return digest.hexdigest()


def scan_inbox(inbox_root: Path) -> list[dict]:
    """枚举 inbox 下所有文件并计算内容哈希。

    跳过隐藏文件/目录（.DS_Store 等）与 __pycache__；断链符号链接跳过。
    返回按路径排序的 [{"path", "sha256", "bytes", "role"}]，path 为 posix 相对路径。
    """
    entries = []
    for path in sorted(Path(inbox_root).rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(inbox_root)
        if any(part.startswith(".") for part in rel.parts):
            continue
        if "__pycache__" in rel.parts:
            continue
        entries.append({
            "path": rel.as_posix(),
            "sha256": _sha256_file(path),
            "bytes": path.stat().st_size,
            "role": classify(rel.as_posix()),
        })
    return entries


def compute_snapshot_id(entries: list[dict]) -> str:
    """材料内容指纹：任何文件的增删改都会改变它（MathModel snapshot 同构）。"""
    payload = "|".join(f"{e['path']}:{e['sha256']}" for e in entries)
    return "INBOX-" + hashlib.sha256(payload.encode("utf-8")).hexdigest()[:12]


def load_old_manifest(manifest_path: Path) -> dict | None:
    """读上一轮 manifest；缺失或 JSON 损坏返回 None（视为无缓存，全量重跑）。"""
    try:
        data = json.loads(Path(manifest_path).read_text(encoding="utf-8", errors="replace"))
        return data if isinstance(data, dict) else None
    except (json.JSONDecodeError, OSError):
        return None


def find_idea_candidates(entries: list[dict], inbox_root: Path) -> list[dict]:
    """role==idea 的文件 → 候选清单（附带骨架检测，防未填模板被误转正）。

    .md/.txt 读文本跑 content_source.idea_is_skeleton（与流水线预检同一道门禁，
    ~40 词阈值）；.docx/.pdf 标 text_readable=False —— 只列不选，转正了
    load_idea_document 也读不出，还会把二进制垃圾写进 idea.md。
    """
    from .content_source import idea_is_skeleton, load_idea_document

    candidates = []
    for entry in entries:
        if entry["role"] != "idea":
            continue
        ext = Path(entry["path"]).suffix.lower()
        if ext in _TEXT_IDEA_EXTS:
            full = Path(inbox_root) / entry["path"]
            text = load_idea_document(str(full))
            skeleton, words = idea_is_skeleton(text)
            candidates.append({**entry, "text_readable": True,
                               "words": words, "skeleton": skeleton})
        else:
            candidates.append({**entry, "text_readable": False,
                               "words": 0, "skeleton": False})
    return candidates


def _idea_md_state(idea_path: Path | None, old_manifest: dict | None) -> str:
    """当前 idea.md 的状态：missing / skeleton / hand-edited / machine-promoted。

    machine-promoted = 内容 sha256 与旧 manifest 记录的 promoted_sha256 一致
    （机器上次转正后作者没动过 → 覆盖它是安全的）。
    """
    if idea_path is None or not Path(idea_path).is_file():
        return "missing"
    from .content_source import idea_is_skeleton, load_idea_document

    text = load_idea_document(str(idea_path))
    skeleton, _words = idea_is_skeleton(text)
    old_promoted = ((old_manifest or {}).get("idea") or {}).get("promoted_sha256")
    if old_promoted and _sha256_file(Path(idea_path)) == old_promoted:
        return "machine-promoted"
    if skeleton:
        return "skeleton"
    return "hand-edited"


def resolve_idea(candidates: list[dict], idea_path: Path | None,
                 forced_rel: str | None = None,
                 old_manifest: dict | None = None) -> dict:
    """idea 转正决策（纯函数，不做 IO）。

    返回 {"resolution", "selected", "promote", "messages"}：
    - none-found        无可用候选（报告指引作者直接写 idea.md）
    - auto-single       恰一个非骨架文本候选 → 自动转正
    - ambiguous         ≥2 候选 → 不猜，列候选表让作者用 --idea 绑定
    - author-bound      --idea 显式绑定（仍受手改保护）
    - existing-kept     idea.md 已存在且非骨架 → 绝不覆盖
    - hand-edit-protected 绑定目标与手改过的 idea.md 冲突 → 拒绝，防一把抹掉人工修订
    """
    state = _idea_md_state(idea_path, old_manifest)
    selectable = [c for c in candidates if c["text_readable"] and not c["skeleton"]]

    def result(resolution, selected=None, promote=False, messages=None):
        return {"resolution": resolution, "selected": selected,
                "promote": promote, "messages": messages or []}

    # idea.md 已是作者手填内容 → 现有文件优先（对齐 input.md/data-index 不覆盖不变量）。
    if state == "hand-edited":
        return result("existing-kept", messages=[
            f"idea.md 已存在且非骨架（{idea_path}），保留不动。",
            "要换来源请先自行备份/删除它，再跑 --intake。",
        ])

    if forced_rel is not None:
        names = {c["path"] for c in candidates}
        if forced_rel not in names:
            return result("none-found", messages=[
                f"--idea 指定的 {forced_rel} 不是盘点出的 idea 候选"
                f"（候选：{sorted(names) or '无'}）。",
                "角色初判只认文件名信号（idea/模型说明/方法/思路/方案…）；"
                "改文件名或手动复制该文件为 idea.md。",
            ])
        target = next(c for c in candidates if c["path"] == forced_rel)
        if not target["text_readable"] or target["skeleton"]:
            why = ("是二进制文档（先转成 md/txt 放回 inbox）"
                   if not target["text_readable"] else "内容疑似未填模板")
            return result("hand-edit-protected", messages=[
                f"--idea 指定的 {forced_rel} {why}，不转正。",
            ])
        return result("author-bound", selected=forced_rel, promote=True)

    if not selectable:
        if candidates:
            return result("none-found", messages=[
                "发现 idea 候选但均不可自动转正（二进制或疑似未填模板），见报告候选表。",
            ])
        return result("none-found", messages=[
            "inbox 里没有 idea 候选（文件名信号：idea/模型说明/方法/思路/方案…）。",
            "直接 cp idea.example.md idea.md 填第 3/4 节；idea 族章节缺它会硬停。",
        ])
    if len(selectable) == 1:
        return result("auto-single", selected=selectable[0]["path"], promote=True)
    listed = "、".join(f"`{c['path']}`" for c in selectable)
    return result("ambiguous", messages=[
        f"发现 {len(selectable)} 个 idea 候选：{listed}。",
        "不自动猜 —— idea.md 被每个 Agent 每阶段全文读，选错污染全篇。",
        "选定后跑：python run.py --intake --idea inbox/<选定文件>",
    ])


def promote_idea(inbox_root: Path, rel: str, idea_path: Path) -> str:
    """把选定候选逐字复制为 idea.md（copy2 不改一个字节）。返回新文件 sha256。"""
    src = Path(inbox_root) / rel
    Path(idea_path).parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, idea_path)
    return _sha256_file(Path(idea_path))


# ── data 对账：与 load_results_store 逐字节同判据 ──────────────────────────

_EXAMPLE_REASON = "loader 跳过 example/template 文件（示例数据不算实验证据）"
_UNREADABLE_REASONS = {
    ".xlsx": "xlsx：loader 只读 .json/.csv，需作者转存 csv/json（不引入 openpyxl）",
    ".xls": "xls：loader 只读 .json/.csv，需作者转存 csv/json",
    ".tsv": "tsv：loader 只读 .json/.csv，需作者转存 csv/json",
    ".mat": "mat：loader 只读 .json/.csv，需作者转存 csv/json",
}


def probe_data_file(path: Path) -> dict:
    """探测单个数据文件是否会被 load_results_store 读入。

    判据刻意复用 number_gate 的私有部件（_flatten/_load_csv 与 example/template
    跳过规则）：报告声称"读得进"必须与真 loader 一致，自造平行逻辑必然漂移。
    返回 {"loadable", "metrics", "reason"}；metrics = 该文件能压平出的数值条数。
    """
    path = Path(path)
    fn = path.name
    if ".example." in fn or fn.endswith(".example") or fn.startswith("_template"):
        return {"loadable": False, "metrics": 0, "reason": _EXAMPLE_REASON}
    ext = path.suffix.lower()
    if ext not in (".json", ".csv"):
        if ext in _UNREADABLE_REASONS:
            return {"loadable": False, "metrics": 0, "reason": _UNREADABLE_REASONS[ext]}
        return {"loadable": False, "metrics": 0,
                "reason": "非 json/csv：不产生数字，仅可作说明材料"}
    stem = path.stem
    parent = path.parent.name
    prefix = parent if (stem.lower() in _GENERIC_STEMS and parent) else (stem or parent)
    store: dict = {}
    try:
        if ext == ".json":
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                data = json.load(f)
            _flatten(data, prefix, store)
        else:
            _load_csv(str(path), prefix, store)
    except Exception as exc:  # 与 loader 同款：坏文件不沉船，如实记原因
        return {"loadable": False, "metrics": 0,
                "reason": f"解析失败（{type(exc).__name__}），loader 会静默跳过"}
    return {"loadable": bool(store), "metrics": len(store), "reason": ""}


def audit_data(entries: list[dict], inbox_root: Path) -> dict:
    """对 role==data 的文件逐个探测，分三类（只判不搬）。"""
    audit = {"loadable": [], "unreadable": [], "excluded": []}
    for entry in entries:
        if entry["role"] != "data":
            continue
        probe = probe_data_file(Path(inbox_root) / entry["path"])
        if probe["loadable"]:
            audit["loadable"].append({"path": entry["path"], "metrics": probe["metrics"]})
        elif "example/template" in probe["reason"]:
            audit["excluded"].append({"path": entry["path"], "reason": probe["reason"]})
        else:
            audit["unreadable"].append({"path": entry["path"], "reason": probe["reason"]})
    return audit


def summarize_code(entries: list[dict], inbox_root: Path) -> dict:
    """代码盘点：语言统计 + 总行数。只读文本数行，不解析不执行不做入口猜测。"""
    langs: dict[str, int] = {}
    total_lines = 0
    files = 0
    for entry in entries:
        if entry["role"] != "code":
            continue
        files += 1
        ext = Path(entry["path"]).suffix.lower() or "(none)"
        langs[ext] = langs.get(ext, 0) + 1
        try:
            with open(Path(inbox_root) / entry["path"], "r",
                      encoding="utf-8", errors="ignore") as f:
                total_lines += sum(1 for _ in f)
        except OSError:
            pass
    return {"files": files, "languages": langs, "total_lines": total_lines}


# ── 报告与 manifest ──────────────────────────────────────────────────────

_NEXT_STEPS = [
    "（若 §2 未转正）写好/绑定 idea 文档 —— idea 族章节缺它会硬停",
    "把 §3 中「会被读入」的结果文件放进 data/（结构见 data/README.md）",
    "写 outline_draft.md 骨架表（每章：章号|标题|type）",
    "python run.py --expand → 审阅 outline.zh.md → python run.py --init",
    "（可选）python run.py --retrieve",
    "python run.py --all --progress",
]

_DATA_NOTE = ("流水线只读 data/。inbox/ 里的文件**不搬进 data/ 就不会进正文**；"
              "搬运是作者的动作，--intake 不代搬（想好了就 mv/cp 过去）。")


def _role_table(entries: list[dict]) -> str:
    counts: dict[str, int] = {}
    for entry in entries:
        counts[entry["role"]] = counts.get(entry["role"], 0) + 1
    hints = {
        "idea": "idea 候选文档（见 §2）", "problem": "题目/赛题类文档",
        "data": "可能的实验数据（见 §3）", "code": "实验代码（见 §5）",
        "reference": "文献材料（见 §4）", "figure": "图片素材",
        "unknown": "未识别（无目录/文件名/扩展名信号）",
    }
    lines = ["| 角色 | 文件数 | 说明 |", "|---|---:|---|"]
    for role in ROLES:
        if counts.get(role):
            lines.append(f"| {role} | {counts[role]} | {hints[role]} |")
    return "\n".join(lines)


def _idea_section(idea_block: dict) -> str:
    lines = []
    resolution = idea_block.get("resolution", "none-found")
    if resolution in ("auto-single", "author-bound"):
        lines.append(f"**已转正**：`{idea_block.get('selected')}` → `idea.md`")
    elif resolution == "existing-kept":
        lines.append("**保留现有**：idea.md 已存在且非骨架，未从 inbox 覆盖。")
    elif resolution == "ambiguous":
        lines.append("**未决（多候选，不猜）**：选定后跑 "
                     "`python run.py --intake --idea inbox/<选定文件>`")
    else:
        lines.append("**未转正**：无可用候选（指引见 §6）。")
    for msg in idea_block.get("messages", []):
        lines.append(f"- {msg}")
    cands = idea_block.get("candidates") or []
    if cands:
        lines += ["", "| 候选 | 词数 | 状态 | 说明 |", "|---|---:|---|---|"]
        for c in cands:
            if not c.get("text_readable"):
                status, note = "只列不选", "二进制文档，先转 md/txt 放回 inbox"
            elif c.get("skeleton"):
                status, note = "疑似模板", "词数低于骨架门禁阈值，先填实质内容"
            else:
                status, note = "可转正", "文件名信号命中 idea 判定"
            lines.append(f"| `{c['path']}` | {c.get('words', 0)} | {status} | {note} |")
    return "\n".join(lines)


def _data_section(audit: dict) -> str:
    lines = ["> " + _DATA_NOTE, ""]
    if audit["loadable"]:
        lines += ["### 会被 load_results_store 读入 —— 放进 data/ 即生效",
                  "| 文件 | 可提数字 |", "|---|---:|"]
        for item in audit["loadable"]:
            lines.append(f"| `{item['path']}` | {item['metrics']} |")
        lines.append("")
    if audit["unreadable"]:
        lines += ["### 读不进 —— 需作者处理", "| 文件 | 原因 |", "|---|---|"]
        for item in audit["unreadable"]:
            lines.append(f"| `{item['path']}` | {item['reason']} |")
        lines.append("")
    if audit["excluded"]:
        lines += ["### loader 会跳过", "| 文件 | 原因 |", "|---|---|"]
        for item in audit["excluded"]:
            lines.append(f"| `{item['path']}` | {item['reason']} |")
        lines.append("")
    if not (audit["loadable"] or audit["unreadable"] or audit["excluded"]):
        lines.append("inbox 里没有数据类文件。data 族章节会拒绝起草（blocking gate）；"
                     "方法章（advisory）不受影响可先写。")
    return "\n".join(lines).rstrip()


def render_report(manifest: dict) -> str:
    """manifest → 人读 Markdown 报告（六节）。"""
    entries = manifest["files"]
    lines = [
        "# inbox 盘点报告",
        "",
        f"- 快照指纹：`{manifest['snapshot_id']}`（{manifest['generated_at']}）",
        f"- inbox/：`{manifest['inbox_root_abs']}` · {manifest['file_count']} 个文件"
        f" · {manifest['total_bytes']} 字节",
        "",
        "## 1. 角色分布", "", _role_table(entries), "",
        "## 2. idea 转正", "", _idea_section(manifest["idea"]), "",
        "## 3. data 对账（只盘点，不搬运）", "", _data_section(manifest["data_audit"]), "",
        "## 4. 参考文献", "",
    ]
    ref = manifest["reference"]
    if ref["dir_present"]:
        lines.append(f"`inbox/reference/` 下发现 {ref['file_count']} 个文件。"
                     "本框架的引用唯一来源（Tier 1）是 `references/bibliography.md`"
                     "（REF-ID 表格行）；inbox 化不改变引用契约。请人工挑选、"
                     "按表格式整理进 `references/bibliography.md`。")
    else:
        lines.append("未发现文献目录（`inbox/reference/`）。引用契约不变："
                     "`references/bibliography.md` 是唯一 Tier 1 来源。")
    code = manifest["code_summary"]
    lines += ["", "## 5. 代码（只读不执行）", "",
              f"{code['files']} 个文件："
              + "、".join(f"{k} ×{v}" for k, v in sorted(code["languages"].items()))
              + f"，共约 {code['total_lines']} 行。仅盘点；本框架不解析、不执行代码。"
              if code["files"] else "inbox 里没有代码文件。", "",
              "## 6. 下一步", ""]
    lines += [f"{i}. {step}" for i, step in enumerate(_NEXT_STEPS, 1)]
    return "\n".join(lines) + "\n"


def write_manifest(manifest: dict, manifest_path: Path) -> None:
    """manifest 原子落盘（tmp + replace，与项目其它产物同一惯例）。"""
    path = Path(manifest_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(f".json.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(manifest, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, path)


# ── 主流程 ──────────────────────────────────────────────────────────────

def _resolve(default, config_attr: str):
    """显式参数优先；否则在调用时读 config 属性（防 from-import 冻结副本）。"""
    if default is not None:
        return default
    import config
    return getattr(config, config_attr)


def run_intake(forced_idea: str | None = None,
               inbox_root: Path | None = None,
               idea_path: Path | None = None,
               state_dir: Path | None = None) -> tuple[int, dict]:
    """`--intake` 主流程。返回 (退出码, manifest)。

    退出码：0 = 盘点完成（含"无候选/保留现有"这类如实告知）；
            1 = 需要作者行动（多候选待绑定 / 绑定被手改保护拒绝 / 前置失败）。
    """
    inbox = Path(_resolve(inbox_root, "INBOX_ROOT"))
    idea_file = Path(_resolve(idea_path, "IDEA_PATH"))
    state = Path(_resolve(state_dir, "INTAKE_STATE_DIR"))
    manifest_path = state / "inbox-manifest.json"
    report_path = state / "inbox-report.md"

    def fail(msg: str) -> tuple[int, dict]:
        print(f"Error: {msg}")
        return 1, {}

    # 前置 1：inbox 必须存在（不自动建目录——跑 --intake 而没有 inbox 是误用，
    # 空目录摆着反而掩盖问题）。
    if not inbox.is_dir():
        return fail(f"找不到 {inbox}。把散装材料（idea 文档/代码/数据/文献）"
                    "放进去再跑 --intake。")
    # 前置 2：inbox 与 data/ 不得嵌套（否则 load_results_store 会把 inbox 里的
    # json/csv 当实验结果扫进去，数字防线被inbox 噪声污染）。
    import config
    data_root = Path(getattr(config, "DATA_ROOT")).resolve()
    inbox_abs = inbox.resolve()
    if inbox_abs == data_root or data_root in inbox_abs.parents or inbox_abs in data_root.parents:
        return fail(f"inbox/（{inbox_abs}）与 data/（{data_root}）互相嵌套；"
                    "请用两个独立目录，防 data loader 误扫 inbox 材料。")

    entries = scan_inbox(inbox)
    snapshot_id = compute_snapshot_id(entries)
    old = load_old_manifest(manifest_path)

    # 幂等检查：材料没变 + 报告在 + 本轮没带 --idea + 转正状态一致 → 跳过。
    # 带 --idea 时永不跳过（作者显式换绑必须生效，哪怕材料一个字节没动）。
    # 转正状态一致分两种：① 上轮机器转正过 → promoted_sha256 须仍匹配 idea.md
    # 现内容；② 上轮是 existing-kept（作者自有 idea.md，intake 不碰）→ idea.md
    # 仍存在且非骨架即可——作者删掉/清空它则不跳过，恢复转正考虑。
    if (not forced_idea
            and old and old.get("snapshot_id") == snapshot_id
            and report_path.is_file()):
        old_idea = old.get("idea", {})
        if old_idea.get("resolution") == "existing-kept":
            from .content_source import idea_is_skeleton
            if (idea_file.is_file()
                    and not idea_is_skeleton(idea_file.read_text(encoding="utf-8"))[0]):
                print(f"inbox 无变化（{snapshot_id}），跳过。报告：{report_path}")
                return 0, old
        elif old_idea.get("promoted_sha256"):
            current_sha = (_sha256_file(idea_file)
                           if idea_file.is_file() else None)
            if current_sha == old_idea["promoted_sha256"]:
                print(f"inbox 无变化（{snapshot_id}），跳过。报告：{report_path}")
                return 0, old

    # idea 转正（唯一的写副作用）。--idea 参数允许三种形态：相对 inbox 的
    # 裸文件名、带 inbox/ 前缀、绝对路径——统一剥成相对路径再比对候选表。
    forced_rel = None
    if forced_idea:
        raw = str(Path(forced_idea))
        raw_abs = Path(raw)
        if raw_abs.is_absolute():
            try:
                forced_rel = raw_abs.resolve().relative_to(inbox_abs).as_posix()
            except ValueError:
                forced_rel = raw  # 绝对路径不在 inbox 下 → 留原样,resolve_idea 会拒
        else:
            parts = Path(raw).parts
            if parts and parts[0] == inbox.name:
                parts = parts[1:]
            forced_rel = Path(*parts).as_posix() if parts else raw
    candidates = find_idea_candidates(entries, inbox)
    decision = resolve_idea(candidates, idea_file, forced_rel, old)
    promoted_sha = None
    if decision["promote"]:
        promoted_sha = promote_idea(inbox, decision["selected"], idea_file)
        print(f"[intake  ] idea 转正 | {decision['selected']} → {idea_file}")

    audit = audit_data(entries, inbox)
    code_summary = summarize_code(entries, inbox)
    ref_count = sum(1 for e in entries if e["role"] == "reference")
    manifest = {
        "schema_version": 1,
        "snapshot_id": snapshot_id,
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "inbox_root": "inbox",
        "inbox_root_abs": str(inbox_abs),
        "file_count": len(entries),
        "total_bytes": sum(e["bytes"] for e in entries),
        "files": entries,
        "idea": {
            "candidates": candidates,
            "forced_by": forced_rel,
            "resolution": decision["resolution"],
            "selected": decision["selected"],
            "promoted_to": str(idea_file) if promoted_sha else None,
            "promoted_sha256": promoted_sha,
            "messages": decision["messages"],
        },
        "data_audit": {**audit, "note": _DATA_NOTE},
        "code_summary": code_summary,
        "reference": {"dir_present": ref_count > 0, "file_count": ref_count},
        "next_steps": list(_NEXT_STEPS),
    }
    # data_probe 并入 files 条目（报告/manifest 双载体，读哪个都不缺信息）。
    loadable_paths = {i["path"]: i["metrics"] for i in audit["loadable"]}
    for entry in manifest["files"]:
        if entry["role"] == "data":
            full = inbox / entry["path"]
            probe = probe_data_file(full)
            entry["data_probe"] = probe

    write_manifest(manifest, manifest_path)
    report_path.write_text(render_report(manifest), encoding="utf-8")

    # 终端摘要。
    role_counts: dict[str, int] = {}
    for e in entries:
        role_counts[e["role"]] = role_counts.get(e["role"], 0) + 1
    print(f"[intake  ] 盘点完成  | {len(entries)} 个文件（"
          + "、".join(f"{r}×{n}" for r, n in sorted(role_counts.items())) + "）")
    for msg in decision["messages"]:
        print(f"[intake  ] idea      | {msg}")
    print(f"[intake  ] data      | 读得进 {len(audit['loadable'])} · "
          f"读不进 {len(audit['unreadable'])} · 跳过 {len(audit['excluded'])}（只盘点不搬运）")
    print(f"[intake  ] 报告      | {report_path}")
    needs_author = decision["resolution"] in ("ambiguous", "hand-edit-protected")
    return (1 if needs_author else 0), manifest
