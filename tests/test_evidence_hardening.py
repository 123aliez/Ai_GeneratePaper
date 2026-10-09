"""演进点②③① 的离线验证:RES 占位符 / Skill 注册表与指纹 / 结构化单轮调用。

不调 API、不装 smolagents(沿用 test_optimizations 的 stub 策略)。
运行: python tests/test_evidence_hardening.py
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

from agents.result_ids import (
    assign_res_ids, merge_res_ids, render_res_ids_md, parse_res_ids_md,
    ensure_res_ids, fill_result_refs, strip_placeholders,
)
import agents.skills_registry as sr
import agents.structured_call as sc

PASSED = []


def check(label, cond, detail=""):
    if not cond:
        raise AssertionError(f"{label}: {detail or 'failed'}")
    PASSED.append(label)


# ── 演进点②:RES-ID 分配 / 合并 / 替换 ─────────────────────────────────
def test_assign_res_ids_stable_and_sorted():
    store = {"run_b.acc": 0.918, "run_a.loss": 0.12, "meta.name": ":exp1"}
    m1 = assign_res_ids(store)
    m2 = assign_res_ids(store)
    check("分配是纯函数(两次调用一致)", m1 == m2)
    check("按 key 字典序分配",
          list(m1) == ["RES-001", "RES-002"]
          and m1["RES-001"]["key"] == "run_a.loss", str(m1))
    check("文本元数据不占 ID", all("meta.name" != v["key"] for v in m1.values()))


def test_merge_preserves_old_ids():
    old_text = render_res_ids_md({"RES-001": {"key": "run_a.loss", "value": 0.12},
                                  "RES-002": {"key": "run_b.acc", "value": 0.918}})
    # data/ 换血:run_a 没了,新增 z_metric;run_b 值更新
    merged = merge_res_ids({"run_b.acc": 0.95, "z_metric": 3.2}, old_text)
    check("消失的 key 不复用其 ID(RES-001 不再指向别的 key)",
          all(v["key"] != "run_a.loss" for v in merged.values()), str(merged))
    check("旧 ID 语义保持(RES-002 仍是 run_b.acc)",
          merged["RES-002"]["key"] == "run_b.acc", str(merged))
    check("值取当前 data/ 读数(ID 是引用、值是事实)",
          merged["RES-002"]["value"] == 0.95, str(merged))
    check("新 key 在最大编号后追加",
          merged["RES-003"]["key"] == "z_metric", str(merged))


def test_ensure_res_ids_roundtrip_and_skip_write():
    root = tempfile.mkdtemp()
    os.makedirs(os.path.join(root, "run_0"))
    with open(os.path.join(root, "run_0", "final_info.json"), "w") as f:
        json.dump({"metrics": {"acc": {"means": 0.918}}}, f)
    m, path = ensure_res_ids(root)
    check("落盘 data/res-ids.md", path is not None and path.is_file())
    first_mtime = path.stat().st_mtime_ns
    m2, _ = ensure_res_ids(root)
    check("内容未变不重写(mtime 不抖动)", path.stat().st_mtime_ns == first_mtime)
    check("解析器读回同一映射",
          parse_res_ids_md(path.read_text(encoding="utf-8"))
          == {rid: v["key"] for rid, v in m2.items()})
    empty_root = tempfile.mkdtemp()
    m3, p3 = ensure_res_ids(empty_root)
    check("空结果库不落盘", m3 == {} and p3 is None)


def test_fill_and_strip():
    res_map = {"RES-001": {"key": "k", "value": 0.918}}
    text, unresolved = fill_result_refs("acc {{RES-001}} vs {{RES-999}}", res_map)
    check("已知 ID 替换为记录值", "0.918" in text and "{{RES-001}}" not in text, text)
    check("未知 ID 显式标记不编值", "⟨未知结果 RES-999⟩" in text, text)
    check("未知 ID 列表返回给调用方", unresolved == ["RES-999"])
    stripped = strip_placeholders("a {{RES-001}} b ⟨未知结果 RES-002⟩ c")
    check("strip 剥掉占位与未知标记", "{{" not in stripped and "⟨" not in stripped, stripped)


def test_number_gate_strips_placeholders():
    # number_gate 集成:占位符编号不得被当成手打数字误报
    from agents.number_gate import run_number_gate
    root = tempfile.mkdtemp()
    os.makedirs(os.path.join(root, "run_0"))
    with open(os.path.join(root, "run_0", "final_info.json"), "w") as f:
        json.dump({"metrics": {"test_accuracy": {"means": 0.918}}}, f)
    d = tempfile.mkdtemp()
    draft = os.path.join(d, "draft.md")
    with open(draft, "w") as f:
        f.write("The test accuracy of {{RES-001}} improves.")
    passed, msgs = run_number_gate(draft, root)
    check("占位符草稿不误报", passed, str(msgs))
    with open(draft, "w") as f:
        f.write("The test accuracy of 82.3% is fabricated.")
    passed, msgs = run_number_gate(draft, root)
    check("手打错值照样被抓", not passed and any("82.3%" in m for m in msgs), str(msgs))


def test_idea_clause_uses_res_ids_when_available():
    import agents.orchestrator as orch
    clause = orch.idea_clause("data", res_ids_path="/x/data/res-ids.md")
    check("res_ids_path 在场 → 数字纪律指向占位符",
          "res-ids.md" in clause and "{{RES-xxx}}" in clause, clause[:300])
    legacy = orch.idea_clause("data", res_ids_path=None)
    check("res_ids_path 缺席 → 退回 data-index 旧表述",
          "data-index.md" in legacy and "RES" not in legacy, legacy[:300])
    idea_c = orch.idea_clause("idea", res_ids_path="/x/data/res-ids.md")
    check("idea 章不带数字纪律", "RES" not in idea_c, idea_c[:200])


# ── 演进点③:Skill 注册表 + 阶段指纹 ───────────────────────────────────
def test_skills_registry_whitelist_and_injection():
    inj = sr.build_skill_injection("draft")
    check("draft 注入块含 4 个白名单 skill",
          all(f"skill: {n}" in inj for n in sr.SKILL_REGISTRY["draft"]), inj[:200])
    check("注入块带 header 且劝阻重复读取",
          "METHODOLOGY SKILLS" in inj and "do not re-read" in inj, inj[:200])
    review_inj = sr.build_skill_injection("review")
    check("review 只装 review-rubric",
          "review-rubric.md" in review_inj and "writing-style" not in review_inj)
    check("manager 白名单为空 → 无注入", sr.build_skill_injection("manager") == "")


def test_skills_fingerprint_changes_with_content():
    # 只动临时副本,不写仓库里的 skills/(测试中断不得留下仓库改动)
    baseline = sr.skills_fingerprint()
    from unittest.mock import patch
    with tempfile.TemporaryDirectory() as tmp:
        skill_root = Path(tmp)
        target = skill_root / sr.SKILL_REGISTRY["review"][0]
        target.write_text("First version", encoding="utf-8")
        with patch.object(sr, "SKILLS_DIR", skill_root):
            fp1 = sr.skills_fingerprint()
            target.write_text("Second version", encoding="utf-8")
            check("skill 内容变化 → 指纹变化", fp1 != sr.skills_fingerprint())
    check("退出临时目录后指纹复原", sr.skills_fingerprint() == baseline)


def test_stage_fingerprint_lifecycle():
    d = tempfile.mkdtemp()
    art = "review-v1.json"
    Path(d, art).write_text("{}", encoding="utf-8")
    # 未记账 → 陈旧
    check("存在但未记账 → 判陈旧",
          sr.stale_stage_artifacts(d, [art], "fpA", {"review": "m1"}) == [art])
    sr.record_stage_fingerprint(d, art, "fpA", "review", "m1")
    check("指纹相符 → 不陈旧",
          sr.stale_stage_artifacts(d, [art], "fpA", {"review": "m1"}) == [])
    check("brief 指纹变 → 判陈旧",
          sr.stale_stage_artifacts(d, [art], "fpB", {"review": "m1"}) == [art])
    check("模型组变 → 判陈旧",
          sr.stale_stage_artifacts(d, [art], "fpA", {"review": "m2"}) == [art])
    check("prompts.py 内容参与指纹(不同 stage 名指纹不同)",
          sr.stage_fingerprint("fpA", "review") != sr.stage_fingerprint("fpA", "draft"))
    # 轮次产物名 → 阶段推断
    check("round 产物名映射到 revise/verify 阶段",
          sr._stage_of("draft-v2.round-2.md") == "revise"
          and sr._stage_of("review-verify.round-1.json") == "verify")


def test_orchestrator_blocks_on_stale_fingerprints():
    """端到端:老产物(无 manifest 记账)在场 → 流水线硬停并列出,不静默复用。"""
    import agents.orchestrator as orch
    import agents.outline as ol
    from agents.content_source import load_idea_document
    sys.path.insert(0, str(ROOT / "tests"))
    from test_pipeline_routing import make_ws_in_outline, set_idea, IDEA_TEXT

    set_idea(IDEA_TEXT)
    folder, outline, _ = make_ws_in_outline(
        "type: results\n\n1. **Results** (~250 words)\n- numbers\n", "03-results")
    # 造一个"改造前生成"的产物:存在,但没有 .stage-manifest.json 记账
    Path(folder, "draft-v1.md").write_text("# old draft\n", encoding="utf-8")
    data_root = make_gate_data()

    class NoCallAgent:
        _offline_test_stub = True
        def __init__(self):
            self.prompts = []
        def run(self, prompt, **kw):
            self.prompts.append(prompt)
            return "should not happen"

    agent = NoCallAgent()
    old_outline = ol.OUTLINE_PATH
    ol.OUTLINE_PATH = Path(outline)
    try:
        orch.DATA_ROOT = data_root
        results = orch.run_4stage_with_progress(agent, agent, folder, None)
    finally:
        ol.OUTLINE_PATH = old_outline
    check("未记账产物 → 零模型调用", not agent.prompts, str(agent.prompts))
    check("返回值记录 stale_stage_artifacts",
          "draft-v1.md" in results.get("stale_stage_artifacts", []), str(results))
    check("route_blocked 给出指纹陈旧原因",
          "fingerprints stale" in results.get("route_blocked", ""), str(results))
    check("产物未被删除(是否重跑由使用者定)",
          Path(folder, "draft-v1.md").exists())


def make_gate_data():
    root = tempfile.mkdtemp()
    os.makedirs(os.path.join(root, "run_0"))
    with open(os.path.join(root, "run_0", "final_info.json"), "w") as f:
        json.dump({"metrics": {"acc": {"means": 0.9}}}, f)
    return root


# ── 演进点①:结构化单轮调用 ────────────────────────────────────────────
def test_extract_json_shapes():
    check("围栏 JSON", sc._extract_json('noise ```json {"a": 1}``` tail') == {"a": 1})
    check("裸 JSON", sc._extract_json('answer: {"b": [1,2]}') == {"b": [1, 2]})
    check("含控制符", sc._extract_json('{"c":\n1}') == {"c": 1})
    check("非 JSON → None", sc._extract_json("no braces here") is None)


def test_run_structured_call_writes_artifact():
    class FakeModel:
        def generate(self, messages):
            class R:
                content = '```json\n{"must_fix": [], "decision": "ACCEPT"}\n```'
            return R()

    d = tempfile.mkdtemp()
    out = os.path.join(d, "review-v1.json")
    data = sc.run_structured_call(FakeModel(), "Review", "SYSTEM", "USER",
                                  out, "schema")
    check("返回解析后的 dict", data == {"must_fix": [], "decision": "ACCEPT"}, str(data))
    check("产物原子落盘且是合法 JSON",
          json.loads(Path(out).read_text(encoding="utf-8")) == data)
    check("无 .tmp 残留", not Path(out + ".tmp").exists())

    class GarbageModel:
        def generate(self, messages):
            class R:
                content = "I cannot produce JSON"
            return R()

    check("不可解析 → None(不抛,由调用方降级)",
          sc.run_structured_call(GarbageModel(), "Review", "s", "u",
                                 os.path.join(d, "x.json"), "schema") is None)


def test_offline_stub_skips_structured_path():
    """离线测试桩(_offline_test_stub)不发真实模型调用 —— 全量套件不触网。"""
    src = Path(ROOT, "agents", "orchestrator.py").read_text(encoding="utf-8")
    check("orchestrator 尊重 _offline_test_stub 标记",
          "_offline_test_stub" in src)
    check("单轮直调有 CodeAgent 兜底(失败回退)",
          "falling back to CodeAgent path" in src)


def test_record_stage_covers_all_candidates():
    """演进点③闭环:流水线跑完后,每个「存在即跳过」候选产物都有 manifest 记账。

    防回归:record_stage 漏接某个产物 → 它永远「存在但未记账」→ 下一章 resume
    时被 stale 检查硬停,用户被迫删文件重跑 —— 记账闭环断了。input.md 例外
    (可能含作者手填内容,不进候选;但生成路径照常记它,见 orchestrator 注释)。
    """
    import agents.orchestrator as orch
    import agents.outline as ol
    sys.path.insert(0, str(ROOT / "tests"))
    from test_pipeline_routing import (
        make_ws_in_outline, set_idea, IDEA_TEXT, make_data, FullRunAgent,
    )
    import io, contextlib

    set_idea(IDEA_TEXT)
    folder, outline, _ = make_ws_in_outline(
        "type: method\n\n1. **Framework** (~200 words)\n- 机制")
    data = make_data({"test_accuracy": 0.817})
    agent = FullRunAgent(folder)
    old = ol.OUTLINE_PATH
    ol.OUTLINE_PATH = Path(outline)
    buf = io.StringIO()
    try:
        orch.DATA_ROOT = data
        with contextlib.redirect_stdout(buf):
            first = orch.run_4stage_with_progress(agent, agent, folder, None)
    finally:
        ol.OUTLINE_PATH = old
    # 首次必须完整成功(此前只查产物存在,Stage 5 校验改坏也发现不了)
    check("首次流水线完整成功(含 Stage 5 交接)",
          first.get("stage5_xchap_ok") is True, str(first.get("stage5_xchap_ok")))
    check("首次跑出终稿", (Path(folder) / "final.md").is_file())

    manifest_path = Path(folder, ".stage-manifest.json")
    check("跑完一章后 manifest 存在", manifest_path.is_file())
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    # 候选产物 = ROUTE_DEPENDENT_ARTIFACTS + 两个 glob 的轮次产物
    candidates = list(orch.ROUTE_DEPENDENT_ARTIFACTS)
    for pattern in orch.ROUTE_DEPENDENT_ARTIFACT_GLOBS:
        candidates.extend(p.name for p in Path(folder).glob(pattern))
    missing = [c for c in candidates
               if (Path(folder) / c).exists() and c not in manifest]
    check("每个存在的候选产物都有记账(record 闭环)", not missing, str(missing))

    # 同指纹 resume:全部 skip、零模型调用、不硬停(记账与检查用同一指纹源)。
    agent2 = FullRunAgent(folder)
    buf2 = io.StringIO()
    ol.OUTLINE_PATH = Path(outline)
    try:
        with contextlib.redirect_stdout(buf2):
            r2 = orch.run_4stage_with_progress(agent2, agent2, folder, None)
    finally:
        ol.OUTLINE_PATH = old
    check("同指纹重跑不硬停", "route_blocked" not in r2, str(r2.get("route_blocked")))
    check("同指纹重跑零模型调用", not agent2.prompts,
          f"calls={len(agent2.prompts)}")


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in tests:
        fn()
        print(f"  ok  {fn.__name__}")
    print(f"\nEVIDENCE HARDENING TESTS PASSED — {len(PASSED)} 项检查全过")
