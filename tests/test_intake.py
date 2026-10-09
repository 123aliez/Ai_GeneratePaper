"""inbox 前处理层（--intake）的离线验证。

不调 API、不装 smolagents（沿用 test_optimizations 的 stub 策略）。
覆盖:classify 角色初判 / sha256 快照幂等 / idea 转正决策表(含多候选不猜、
手改保护、模板降级) / data 对账与真 loader 同判据 / 边界(缺失、空、嵌套) /
产物契约(manifest + 报告)。
运行: python tests/test_intake.py
"""
import json
import os
import sys
import tempfile
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

try:
    import smolagents  # noqa: F401
except ModuleNotFoundError:
    stub = types.ModuleType("smolagents")

    def _tool(fn):
        return fn

    class _Agent:
        def __init__(self, *a, **kw):
            self.kwargs = kw
            self.tools = kw.get("tools", [])

    stub.tool = _tool
    stub.CodeAgent = _Agent
    stub.Model = object
    stub.OpenAIModel = object
    sys.modules["smolagents"] = stub

from agents import inbox_intake as ii

PASSED = []


def check(label, cond, detail=""):
    if not cond:
        raise AssertionError(f"{label}: {detail or 'failed'}")
    PASSED.append(label)


def make_root():
    """伪项目根:inbox/ + data/ + var/intake/ 三件套互不嵌套。"""
    root = Path(tempfile.mkdtemp())
    (root / "inbox").mkdir()
    (root / "data").mkdir()
    return root


def write(root: Path, rel: str, text: str) -> Path:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return p


# 长到不会被骨架门禁误杀的 idea 文档。
IDEA_BODY = ("我们提出 SpecNet 频域通道重标定模块,对特征图做 FFT 后按径向频带聚合能量,"
             "轻量 MLP 生成门控系数,在三个细粒度基准上验证一致性增益,参数增量仅 0.03M。"
             "方法设计包含频带划分、门控参数化与插入位置三个部分,给出完整推导。")


# ── classify:目录信号 > 文件名信号 > 扩展名兜底 ─────────────────────────
def test_classify_roles():
    cases = {
        "code/x.md": "code", "src/main.py": "code", "代码/说明.md": "code",
        "data/a.json": "data", "results/r.csv": "data", "数据/b.xlsx": "data",
        "reference/p.md": "reference", "references/q.md": "reference",
        "文献/论文.md": "reference",
        "X模型说明文档.md": "idea", "idea.md": "idea", "方法.md": "idea",
        "method_notes.txt": "idea", "方案_v2.md": "idea",
        "题目.md": "problem", "problem.pdf": "problem",
        "a.py": "code", "b.ipynb": "code", "c.xlsx": "data", "d.mat": "data",
        "e.png": "figure", "f.zip": "unknown", "g.docx": "unknown",
    }
    for path, want in cases.items():
        check(f"classify({path}) == {want}", ii.classify(path) == want,
              f"got {ii.classify(path)}")
    # 目录信号压过文件名信号:code/ 里的"方法.md"是代码材料,不是 idea 候选。
    check("目录信号压过文件名(code/方法.md → code)",
          ii.classify("code/方法.md") == "code")
    # 文件名信号只在文档扩展名上生效:results/方法.xlsx 是 data,不是 idea。
    check("文件名信号仅限文档扩展名(方法.xlsx → data)",
          ii.classify("方法.xlsx") == "data")


# ── 快照与幂等 ──────────────────────────────────────────────────────────
def test_snapshot_id_stable_and_sensitive():
    root = make_root()
    write(root, "inbox/a.md", "one")
    write(root, "inbox/code/b.py", "print(1)")
    e1 = ii.scan_inbox(root / "inbox")
    check("两次扫描同 snapshot_id",
          ii.compute_snapshot_id(e1) == ii.compute_snapshot_id(ii.scan_inbox(root / "inbox")))
    write(root, "inbox/a.md", "two")
    e2 = ii.scan_inbox(root / "inbox")
    check("内容变化 → snapshot_id 变",
          ii.compute_snapshot_id(e1) != ii.compute_snapshot_id(e2))
    write(root, "inbox/c.txt", "new file")
    e3 = ii.scan_inbox(root / "inbox")
    check("新增文件 → snapshot_id 变",
          ii.compute_snapshot_id(e2) != ii.compute_snapshot_id(e3))
    # 隐藏文件与 __pycache__ 不入快照
    write(root, "inbox/.DS_Store", "junk")
    write(root, "inbox/code/__pycache__/x.pyc", "junk")
    e4 = ii.scan_inbox(root / "inbox")
    check("隐藏/__pycache__ 文件不入快照",
          ii.compute_snapshot_id(e3) == ii.compute_snapshot_id(e4)
          and all(".DS_Store" not in e["path"] for e in e4))


def test_run_intake_idempotent_skip_and_repair():
    root = make_root()
    write(root, "inbox/模型说明.md", IDEA_BODY)
    write(root, "inbox/data/run.json", '{"metrics": {"acc": {"means": 0.9}}}')
    state = root / "var" / "intake"
    code1, m1 = ii.run_intake(inbox_root=root / "inbox",
                              idea_path=root / "idea.md", state_dir=state)
    check("首次转正成功", code1 == 0 and (root / "idea.md").is_file())
    manifest_mtime = (state / "inbox-manifest.json").stat().st_mtime_ns
    code2, m2 = ii.run_intake(inbox_root=root / "inbox",
                              idea_path=root / "idea.md", state_dir=state)
    check("材料未变第二次跳过", code2 == 0
          and (state / "inbox-manifest.json").stat().st_mtime_ns == manifest_mtime)
    # 作者删了 idea.md → 只重做转正,修回来
    (root / "idea.md").unlink()
    code3, _ = ii.run_intake(inbox_root=root / "inbox",
                             idea_path=root / "idea.md", state_dir=state)
    check("idea.md 被删后重跑修复转正",
          code3 == 0 and (root / "idea.md").is_file())
    # 旧 manifest 损坏 → 全量重跑不炸
    (state / "inbox-manifest.json").write_text("{broken", encoding="utf-8")
    code4, _ = ii.run_intake(inbox_root=root / "inbox",
                             idea_path=root / "idea.md", state_dir=state)
    check("manifest 损坏不炸,全量重跑",
          code4 == 0 and json.loads((state / "inbox-manifest.json")
                                    .read_text(encoding="utf-8"))["snapshot_id"])


# ── idea 转正决策表 ─────────────────────────────────────────────────────
def test_idea_promotion_decisions():
    root = make_root()
    state = root / "var" / "intake"

    # 单一非骨架候选 → auto-single,逐字复制
    write(root, "inbox/模型说明.md", IDEA_BODY)
    code, _ = ii.run_intake(inbox_root=root / "inbox",
                            idea_path=root / "idea.md", state_dir=state)
    check("单候选自动转正,退出 0", code == 0)
    check("idea.md 与候选逐字节一致",
          (root / "idea.md").read_bytes() == (root / "inbox/模型说明.md").read_bytes())

    # 多候选 → ambiguous 不猜,退出 1
    root2 = make_root()
    write(root2, "inbox/模型说明.md", IDEA_BODY)
    write(root2, "inbox/方案.md", IDEA_BODY.replace("SpecNet", "PlanB") * 2)
    code2, m2 = ii.run_intake(inbox_root=root2 / "inbox",
                              idea_path=root2 / "idea.md", state_dir=root2 / "var/intake")
    check("双候选不猜,退出 1", code2 == 1)
    check("多候选不写 idea.md", not (root2 / "idea.md").exists())
    check("报告列出两个候选",
          m2["idea"]["resolution"] == "ambiguous" and len(m2["idea"]["candidates"]) == 2)
    # --idea 绑定解歧
    code3, m3 = ii.run_intake(forced_idea="方案.md", inbox_root=root2 / "inbox",
                              idea_path=root2 / "idea.md", state_dir=root2 / "var/intake")
    check("绑定解歧并转正",
          code3 == 0 and (root2 / "idea.md").is_file()
          and m3["idea"]["resolution"] == "author-bound")
    handwritten = ("作者手工重写的 idea 文档，内容足够长以越过骨架门禁的四十词阈值，"
                   "代表真实的作者手改场景，验证 --intake 不会把它抹掉。")
    (root2 / "idea.md").write_text(handwritten, encoding="utf-8")
    code4, m4 = ii.run_intake(forced_idea="模型说明.md", inbox_root=root2 / "inbox",
                              idea_path=root2 / "idea.md", state_dir=root2 / "var/intake")
    check("手改后的 idea.md 不被覆盖",
          code4 == 0 and (root2 / "idea.md").read_text(encoding="utf-8") == handwritten
          and m4["idea"]["resolution"] == "existing-kept")
    # 机器转正后(sha 匹配旧 manifest)允许换绑
    (root2 / "idea.md").unlink()
    ii.run_intake(forced_idea="模型说明.md", inbox_root=root2 / "inbox",
                  idea_path=root2 / "idea.md", state_dir=root2 / "var/intake")
    code5, m5 = ii.run_intake(forced_idea="方案.md", inbox_root=root2 / "inbox",
                              idea_path=root2 / "idea.md", state_dir=root2 / "var/intake")
    check("machine-promoted 状态允许换绑",
          code5 == 0 and (root2 / "idea.md").read_text(encoding="utf-8").startswith("我们提出 PlanB"))


def test_idea_skeleton_and_binary_candidates():
    root = make_root()
    # 模板内容(<40 词)→ 不自动选（文件名要走 idea 信号,如"模型说明"）
    write(root, "inbox/模型说明.md", "短内容")
    code, m = ii.run_intake(inbox_root=root / "inbox",
                            idea_path=root / "idea.md", state_dir=root / "var/intake")
    check("模板候选不转正(none-found)",
          code == 0 and not (root / "idea.md").exists()
          and m["idea"]["resolution"] == "none-found")
    check("候选表标注疑似模板",
          m["idea"]["candidates"][0]["skeleton"] is True)
    # 二进制候选只列不选
    root2 = make_root()
    (root2 / "inbox/模型说明.pdf").write_bytes(b"%PDF-1.4 fake")
    code2, m2 = ii.run_intake(inbox_root=root2 / "inbox",
                              idea_path=root2 / "idea.md", state_dir=root2 / "var/intake")
    check("二进制候选只列不选",
          code2 == 0 and not (root2 / "idea.md").exists()
          and m2["idea"]["candidates"][0]["text_readable"] is False)
    # --idea 指向二进制 → 拒绝
    code3, m3 = ii.run_intake(forced_idea="模型说明.pdf", inbox_root=root2 / "inbox",
                              idea_path=root2 / "idea.md", state_dir=root2 / "var/intake")
    check("绑定二进制候选被拒",
          code3 == 1 and not (root2 / "idea.md").exists()
          and m3["idea"]["resolution"] == "hand-edit-protected")


# ── data 对账:与 load_results_store 同判据 ──────────────────────────────
def test_data_audit_matches_real_loader():
    from agents.number_gate import load_results_store

    root = make_root()
    write(root, "inbox/data/run_a/final_info.json",
          '{"metrics": {"acc": {"means": 0.9}, "loss": {"means": 0.1}}}')
    write(root, "inbox/data/run_b.json", '{"v": 0.5}')
    write(root, "inbox/data/wide.csv", "name,acc\nalpha,0.8\nbeta,0.7\n")
    write(root, "inbox/data/_template.csv", "a,b\n1,2\n")
    write(root, "inbox/data/附件.xlsx", "binary-ish")
    write(root, "inbox/data/说明.md", "文字说明")
    entries = ii.scan_inbox(root / "inbox")
    audit = ii.audit_data(entries, root / "inbox")

    loadable = {i["path"]: i["metrics"] for i in audit["loadable"]}
    check("json 压平计数正确", loadable.get("data/run_a/final_info.json") == 2,
          str(loadable))
    check("csv 宽表读入", "data/wide.csv" in loadable)
    check("xlsx 归入读不进",
          any("附件.xlsx" in i["path"] for i in audit["unreadable"]))
    check("模板文件归入跳过",
          any(i["path"] == "data/_template.csv" for i in audit["excluded"]))
    check("md 说明归入读不进",
          any(i["path"] == "data/说明.md" for i in audit["unreadable"]))

    # 同判据回归:把可读文件搬进 data/ 后,真 loader 的条数与 probe 一致。
    import shutil
    shutil.copytree(root / "inbox/data", root / "data", dirs_exist_ok=True)
    real = load_results_store(str(root / "data"))
    real_metrics = len([k for k in real if not k.startswith(":")])
    probe_total = sum(loadable.values())
    check("probe 计数与真 loader 一致",
          probe_total == real_metrics,
          f"probe={probe_total} loader={real_metrics} ({sorted(real)})")


# ── 边界 ────────────────────────────────────────────────────────────────
def test_boundaries():
    # inbox 不存在 → 退出 1
    root = make_root()
    code, _ = ii.run_intake(inbox_root=root / "nope",
                            idea_path=root / "idea.md", state_dir=root / "var/intake")
    check("inbox 不存在 → 退出 1", code == 1)
    # 空 inbox → 退出 0 + 报告生成
    code2, m2 = ii.run_intake(inbox_root=root / "inbox",
                              idea_path=root / "idea.md", state_dir=root / "var/intake")
    check("空 inbox → 退出 0 且报告生成",
          code2 == 0 and (root / "var/intake/inbox-report.md").is_file())
    # 嵌套 data → 前置拒绝(config.DATA_ROOT 由调用方环境决定,这里直接用
    # probe/scan 层面测 run_intake 的嵌套守卫:构造 inbox == data 父子关系)
    import config as _cfg
    old_data_root = _cfg.DATA_ROOT
    try:
        _cfg.DATA_ROOT = (root / "inbox" / "data")  # inbox/data 即 data 根:嵌套
        write(root, "inbox/data/x.json", "{}")
        code3, _ = ii.run_intake(inbox_root=root / "inbox",
                                 idea_path=root / "idea.md",
                                 state_dir=root / "var/intake")
        check("inbox 与 data 嵌套 → 退出 1", code3 == 1)
    finally:
        _cfg.DATA_ROOT = old_data_root


# ── 产物契约 ────────────────────────────────────────────────────────────
def test_artifact_contract():
    root = make_root()
    write(root, "inbox/模型说明.md", IDEA_BODY)
    write(root, "inbox/code/train.py", "print(1)\nprint(2)\n")
    write(root, "inbox/code/run.sh", "echo hi\n")
    write(root, "inbox/reference/ref.md", "# Paper\nbody\n")
    code, m = ii.run_intake(inbox_root=root / "inbox",
                            idea_path=root / "idea.md", state_dir=root / "var/intake")
    check("盘点退出 0", code == 0)

    manifest = json.loads((root / "var/intake/inbox-manifest.json")
                          .read_text(encoding="utf-8"))
    for key in ("schema_version", "snapshot_id", "generated_at", "file_count",
                "total_bytes", "files", "idea", "data_audit", "code_summary",
                "reference", "next_steps"):
        check(f"manifest 含 {key}", key in manifest, str(sorted(manifest)))
    check("data_probe 并入 files 条目",
          all("data_probe" in f for f in manifest["files"] if f["role"] == "data"))
    check("code_summary 语言统计",
          manifest["code_summary"]["languages"] == {".py": 1, ".sh": 1}
          and manifest["code_summary"]["total_lines"] == 3,
          str(manifest["code_summary"]))
    check("reference 计数", manifest["reference"] == {"dir_present": True, "file_count": 1})

    report = (root / "var/intake/inbox-report.md").read_text(encoding="utf-8")
    for heading in ("## 1. 角色分布", "## 2. idea 转正", "## 3. data 对账",
                    "## 4. 参考文献", "## 5. 代码", "## 6. 下一步"):
        check(f"报告含 {heading}", heading in report)
    check("报告含不搬运提示", "不搬进 data/ 就不会进正文" in report)
    check("报告含 --expand 指引", "--expand" in report)
    check("报告含转正结果", "已转正" in report and "模型说明.md" in report)


def test_existing_kept_idempotent_skip_and_recovery():
    """existing-kept 路径的幂等(真实烟测 2026-10-09 暴露的缺口)。

    作者自有 idea.md(非骨架)时 intake 不碰它(promoted_sha256=None),旧幂等
    条件要求 promoted_sha256 存在 → 每次重跑都全量重扫重写报告。修复后:
    材料不变 + idea.md 仍非骨架 → 跳过;作者删掉 idea.md → 不跳过,恢复转正。
    """
    root = make_root()
    write(root, "inbox/模型说明.md", IDEA_BODY)
    write(root, "idea.md", "作者自有 idea 文档，内容足够长以越过骨架门禁的四十词阈值，"
                          "代表已有的真实 idea.md，intake 不得覆盖。")
    state = root / "var" / "intake"
    code1, m1 = ii.run_intake(inbox_root=root / "inbox",
                              idea_path=root / "idea.md", state_dir=state)
    check("existing-kept 首跑退出 0",
          code1 == 0 and m1["idea"]["resolution"] == "existing-kept"
          and m1["idea"]["promoted_sha256"] is None)
    check("existing-kept 不写 inbox 候选", (root / "idea.md").read_text(
        encoding="utf-8").startswith("作者自有"))
    mtime = (state / "inbox-manifest.json").stat().st_mtime_ns
    code2, m2 = ii.run_intake(inbox_root=root / "inbox",
                              idea_path=root / "idea.md", state_dir=state)
    check("材料未变第二次跳过(manifest mtime 不变)",
          code2 == 0 and (state / "inbox-manifest.json").stat().st_mtime_ns == mtime)
    # 作者删掉自有 idea.md → 幂等不再成立,恢复考虑转正(auto-single)
    (root / "idea.md").unlink()
    code3, m3 = ii.run_intake(inbox_root=root / "inbox",
                              idea_path=root / "idea.md", state_dir=state)
    check("idea.md 被删后不再跳过,恢复转正",
          code3 == 0 and (root / "idea.md").is_file()
          and (root / "idea.md").read_bytes()
          == (root / "inbox/模型说明.md").read_bytes())
    # 转正后再跑 → 回到 machine-promoted 幂等分支,同样跳过
    mtime4 = (state / "inbox-manifest.json").stat().st_mtime_ns
    code4, _ = ii.run_intake(inbox_root=root / "inbox",
                             idea_path=root / "idea.md", state_dir=state)
    check("转正后幂等跳过仍然生效",
          code4 == 0
          and (state / "inbox-manifest.json").stat().st_mtime_ns == mtime4)


def test_zip_flagged_in_report():
    # .zip 归 unknown;报告未知角色表不崩、给出提示(v1 不自动解压)。
    root = make_root()
    write(root, "inbox/data.zip", "PK\x03\x04")
    write(root, "inbox/模型说明.md", IDEA_BODY)
    code, m = ii.run_intake(inbox_root=root / "inbox",
                            idea_path=root / "idea.md", state_dir=root / "var/intake")
    check("含 zip 的 inbox 正常盘点", code == 0)
    entry = next(f for f in m["files"] if f["path"] == "data.zip")
    check("zip 归 unknown", entry["role"] == "unknown")


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in tests:
        fn()
        print(f"  ok  {fn.__name__}")
    print(f"\nINTAKE TESTS PASSED — {len(PASSED)} 项检查全过")
