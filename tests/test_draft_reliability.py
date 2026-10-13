"""初稿可靠性 8 项修复的离线验证(不调 API)。

1 结果库:seed 等身份列进 key;重复 key / 读不了的文件 → 拒绝生成 RES 目录
2 阶段指纹含 idea.md / 数值结果库;--all 跳过已完成章前先验陈旧
3 章节运行期间 write_file 只能写本章目录
4 "生成完成" 与 "核验通过" 分离(verification.md)
5 Review 判定只认布尔 true;到上限时缺失/损坏的核验项全部上报
6 Stage 4 只基于 draft-v2 定稿
7 RES-ID 退役后永不复用,同 key 重现恢复原号
8 本地 skill 由编排器注入起草/评审提示词

运行: python tests/test_draft_reliability.py
"""
import contextlib
import io
import json
import os
import sys
import tempfile
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

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

import agents.orchestrator as orch
import agents.outline as ol
import agents.skills_registry as sr
import agents.tools as tools
from agents.number_gate import (load_results_store, load_results_store_report,
                                ResultsStoreError)
from agents.result_ids import ensure_res_ids, parse_res_ids_md, parse_retired_ids
from test_pipeline_routing import (make_ws_in_outline, set_idea, IDEA_TEXT,
                                   make_data, FullRunAgent)

PASSED = []


def check(label, cond, detail=""):
    if not cond:
        raise AssertionError(f"{label}: {detail or 'failed'}")
    PASSED.append(label)


def write(path, text):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(text, encoding="utf-8")


def run_chapter(agent, folder, outline, data_root):
    """在 outline 上下文里跑一章,吞掉进度输出,返回 (results, stdout)。"""
    old = ol.OUTLINE_PATH
    ol.OUTLINE_PATH = Path(outline)
    buf = io.StringIO()
    try:
        orch.DATA_ROOT = data_root
        with contextlib.redirect_stdout(buf):
            results = orch.run_4stage_with_progress(agent, agent, folder, None)
    finally:
        ol.OUTLINE_PATH = old
    return results, buf.getvalue()


def staleness(folder, outline, data_root):
    old = ol.OUTLINE_PATH
    ol.OUTLINE_PATH = Path(outline)
    try:
        orch.DATA_ROOT = data_root
        return orch.chapter_staleness(folder)
    finally:
        ol.OUTLINE_PATH = old


# ── 1. 结果库 ────────────────────────────────────────────────────────────
def test_seed_columns_become_part_of_key():
    d = tempfile.mkdtemp()
    write(f"{d}/exp.csv", "method,seed,acc\nours,0,0.91\nours,1,0.93\nbase,0,0.80\n")
    write(f"{d}/long.csv", "metric,seed,value\nacc,0,0.5\nacc,1,0.6\n")
    write(f"{d}/plain.csv", "method,acc\nours,0.91\nbase,0.80\n")
    store, issues = load_results_store_report(d)
    check("宽表 seed 进 key,两个 seed 都保留",
          store.get("exp.ours.seed=0.acc") == 0.91 and store.get("exp.ours.seed=1.acc") == 0.93,
          str(store))
    check("seed 列不再被当成指标值", "exp.ours.seed" not in store, str(store))
    check("长表 seed 进 key",
          store.get("long.seed=0.acc") == 0.5 and store.get("long.seed=1.acc") == 0.6, str(store))
    check("无身份列的文件 key 与改动前一致",
          store.get("plain.ours.acc") == 0.91 and store.get("plain.base.acc") == 0.80, str(store))
    check("合法数据无 issues", not issues["duplicates"] and not issues["unreadable"], str(issues))


def test_ambiguous_store_refuses_res_catalog():
    d = tempfile.mkdtemp()
    write(f"{d}/bad.csv", "method,acc\nours,0.91\nours,0.93\n")
    write(f"{d}/a/run_0/final_info.json", '{"acc": {"means": 0.7}}')
    write(f"{d}/b/run_0/final_info.json", '{"acc": {"means": 0.8}}')
    write(f"{d}/dupkey.json", '{"x": 1, "x": 2}')
    write(f"{d}/broken.json", "{broken")
    _store, issues = load_results_store_report(d)
    dup_keys = {k for k, _a, _b in issues["duplicates"]}
    check("CSV 同标签多行 → 重复", "bad.ours.acc" in dup_keys, str(dup_keys))
    check("同名运行目录 → 重复", "run_0.acc" in dup_keys, str(dup_keys))
    check("JSON 对象内重复键 → 重复", "x" in dup_keys, str(dup_keys))
    check("读不了的文件被报告(相对路径)",
          [p for p, _e in issues["unreadable"]] == ["broken.json"], str(issues["unreadable"]))
    check("宽松接口不抛异常(向后兼容)", isinstance(load_results_store(d), dict))
    try:
        ensure_res_ids(d)
        raised = None
    except ResultsStoreError as exc:
        raised = str(exc)
    check("ensure_res_ids 拒绝生成目录", raised is not None)
    check("报错列出冲突 key 与修法", "bad.ours.acc" in raised and "seed" in raised, raised)
    check("拒绝时不落盘 res-ids.md", not Path(d, "res-ids.md").exists())


def test_same_value_duplicate_is_not_an_issue():
    d = tempfile.mkdtemp()
    write(f"{d}/t.csv", "method,acc\nours,0.91\nours,0.91\n")
    _store, issues = load_results_store_report(d)
    check("同 key 同值不算歧义", not issues["duplicates"], str(issues))


# ── 7. RES-ID 退役 ───────────────────────────────────────────────────────
def test_retired_ids_are_never_reused():
    d = tempfile.mkdtemp()
    write(f"{d}/r.json", json.dumps({"a": 1.0, "b": 2.0, "c": 3.0}))
    m1, path = ensure_res_ids(d)
    check("首次分配 001-003", sorted(m1) == ["RES-001", "RES-002", "RES-003"], str(m1))
    # 删掉编号最大的 key,写回目录
    write(f"{d}/r.json", json.dumps({"a": 1.0, "b": 2.0}))
    m2, _ = ensure_res_ids(d)
    text = path.read_text(encoding="utf-8")
    check("消失的 key 不在活跃表", "RES-003" not in m2 and "RES-003" not in parse_res_ids_md(text))
    check("消失的 ID 进退役列表", parse_retired_ids(text) == {"RES-003": "r.c"}, text)
    # 新 key 出现:不能拿到 003
    write(f"{d}/r.json", json.dumps({"a": 1.0, "b": 2.0, "d": 4.0}))
    m3, _ = ensure_res_ids(d)
    check("新 key 从历史最大编号之后分配",
          m3.get("RES-004", {}).get("key") == "r.d" and "RES-003" not in m3, str(m3))
    # 原 key 重新出现:恢复原号
    write(f"{d}/r.json", json.dumps({"a": 1.0, "b": 2.0, "c": 9.0, "d": 4.0}))
    m4, _ = ensure_res_ids(d)
    check("同 key 重现恢复原 ID,值取当前 data/",
          m4.get("RES-003") == {"key": "r.c", "value": 9.0}, str(m4))
    check("恢复后退役列表清空", parse_retired_ids(path.read_text(encoding="utf-8")) == {})


# ── 5. Review 判定 ───────────────────────────────────────────────────────
def test_resolved_must_be_boolean_true():
    frozen = [{"id": "MF1"}, {"id": "MF2"}]
    for bad in ("false", "true", 1, None):
        verdict = {"items": [{"id": "MF1", "resolved": True},
                             {"id": "MF2", "resolved": bad}]}
        ok, *_ = orch._checklist_verdict(frozen, verdict)
        check(f"resolved={bad!r} 不算已解决", ok is False)
    ok, *_ = orch._checklist_verdict(frozen, {"items": [{"id": "MF1", "resolved": True},
                                                        {"id": "MF2", "resolved": True}]})
    check("全部为布尔 true → 通过", ok is True)


def test_escalation_covers_missing_and_corrupt_verdicts():
    frozen = [{"id": "MF1", "location": "s1", "issue": "i1"},
              {"id": "MF2", "location": "s2", "issue": "i2"}]
    d = tempfile.mkdtemp()
    write(f"{d}/review-verify.round-4.json",
          json.dumps({"items": [{"id": "MF1", "resolved": True}]}))
    orch._escalate_unresolved(d, 4, frozen)
    todo = Path(d, "todo.md").read_text(encoding="utf-8")
    check("verdict 漏报的 id 也上报", "[MF2]" in todo and "[MF1]" not in todo, todo)

    d2 = tempfile.mkdtemp()
    write(f"{d2}/review-verify.round-4.json", "not json at all")
    orch._escalate_unresolved(d2, 4, frozen)
    todo2 = Path(d2, "todo.md").read_text(encoding="utf-8")
    check("verdict 损坏 → 全部冻结项上报", "[MF1]" in todo2 and "[MF2]" in todo2, todo2)


def test_checklist_status_recomputed_from_artifacts():
    d = tempfile.mkdtemp()
    check("review-v1.json 不可解析 → 未核验", orch.checklist_status(d) != [])
    write(f"{d}/review-v1.json", json.dumps({"must_fix": [{"id": "MF1"}]}))
    check("有清单但无核验记录 → 未核验", "没有任何核验记录" in orch.checklist_status(d)[0])
    write(f"{d}/review-verify.round-1.json", json.dumps({"items": [{"id": "MF1", "resolved": False}]}))
    write(f"{d}/review-verify.round-2.json", json.dumps({"items": [{"id": "MF1", "resolved": True}]}))
    check("以最新一轮为准(第 2 轮已解决)", orch.checklist_status(d) == [])
    write(f"{d}/review-v1.json", json.dumps({"must_fix": []}))
    check("没有 MUST FIX → 通过", orch.checklist_status(d) == [])


# ── 2. 输入指纹 ──────────────────────────────────────────────────────────
def test_inputs_fingerprint_scope():
    idea = Path(tempfile.mkdtemp()) / "idea.md"
    idea.write_text("v1", encoding="utf-8")
    data = tempfile.mkdtemp()
    write(f"{data}/r.json", '{"acc": 0.5}')
    f_idea = sr.inputs_fingerprint("idea", idea, data)
    f_data = sr.inputs_fingerprint("data", idea, data)
    check("idea 章只依赖 idea.md", set(f_idea) == {"idea"}, str(f_idea))
    check("data 章依赖 idea.md + 结果库", set(f_data) == {"idea", "data"}, str(f_data))
    write(f"{data}/r.json", '{"acc": 0.6}')
    check("结果数值变 → data 章指纹变", sr.inputs_fingerprint("data", idea, data) != f_data)
    check("结果数值变 → idea 章指纹不变", sr.inputs_fingerprint("idea", idea, data) == f_idea)
    write(f"{data}/res-ids.md", "program written")
    write(f"{data}/plot.png", "binary")
    f_data2 = sr.inputs_fingerprint("data", idea, data)
    check("程序写的 md / 图片不影响 data 指纹",
          sr.inputs_fingerprint("data", idea, data) == f_data2)
    idea.write_text("v2", encoding="utf-8")
    check("idea.md 变 → 指纹变", sr.inputs_fingerprint("idea", idea, data) != f_idea)

    d = tempfile.mkdtemp()
    write(f"{d}/final.md", "x")
    sr.record_stage_fingerprint(d, "final.md", "fp", "final", "m", f_idea)
    check("同输入 → 不陈旧", sr.stale_stage_artifacts(d, ["final.md"], "fp", {"final": "m"}, f_idea) == [])
    f_new = sr.inputs_fingerprint("idea", idea, data)
    check("输入变 → 陈旧", sr.stale_stage_artifacts(d, ["final.md"], "fp", {"final": "m"}, f_new) == ["final.md"])
    check("能说明是 idea.md 变了", sr.changed_inputs(d, ["final.md"], f_new) == ["idea.md"])


def test_idea_change_hard_stops_and_is_seen_by_run_all_check():
    """端到端:跑完一章 → 改 idea.md → 单章重跑硬停、--all 的跳过检查也看得见。"""
    idea_path = set_idea(IDEA_TEXT)
    folder, outline, _ = make_ws_in_outline(
        "type: method\n\n1. **Framework** (~200 words)\n- 机制")
    data = make_data({"test_accuracy": 0.817})
    first, _out = run_chapter(FullRunAgent(folder), folder, outline, data)
    check("首次完整跑通", first.get("stage5_xchap_ok") is True, str(first.get("route_blocked")))
    check("未改输入 → --all 可安全跳过", staleness(folder, outline, data) == [])

    idea_path.write_text(IDEA_TEXT + "\n## Extra claim\nA new mechanism detail.\n",
                         encoding="utf-8")
    reasons = staleness(folder, outline, data)
    check("改了 idea.md → --all 跳过检查报陈旧", reasons and "idea.md" in reasons[0], str(reasons))
    agent = FullRunAgent(folder)
    r2, out2 = run_chapter(agent, folder, outline, data)
    check("单章重跑硬停", "fingerprints stale" in r2.get("route_blocked", ""), str(r2))
    check("硬停零模型调用", not agent.prompts, f"calls={len(agent.prompts)}")
    check("硬停信息说明 idea.md 已变化", "作者输入已变化: idea.md" in out2)
    check("不删除任何产物", Path(folder, "final.md").is_file())

    idea_path.write_text(IDEA_TEXT, encoding="utf-8")
    check("改回原 idea → 恢复可跳过", staleness(folder, outline, data) == [])


def test_run_all_stops_on_stale_completed_chapter():
    import run as run_mod
    ws = tempfile.mkdtemp()
    os.makedirs(os.path.join(ws, "01-method"))
    calls = []
    out, rc = _run_all_patched(ws, calls, handoff=True,
                               staleness=lambda folder: ["作者输入已变化: idea.md;陈旧产物: final.md"])
    check("陈旧的已完成章 → --all 停止(非 0)", rc == 1, out[-400:])
    check("不会把它当成已完成跳过", "已完成,跳过" not in out, out[-400:])
    check("打印陈旧原因", "idea.md" in out and "[陈旧] 需重跑" in out, out[-400:])
    check("陈旧时不调流水线", not calls)


def _run_all_patched(ws, calls, handoff, staleness, mode="experiment"):
    """在桩环境里跑 run_all_chapters:单章 01-method,已完成;返回 (stdout, rc)。"""
    import run as run_mod
    saved = (run_mod.outline_chapters_in_order, run_mod.chapter_is_complete,
             run_mod.PAPER_ROOT, run_mod.run_4stage_with_progress, run_mod.PAPER_MODE,
             orch.chapter_staleness, orch.handoff_is_current)
    run_mod.outline_chapters_in_order = lambda: (["01-method"], None)
    run_mod.chapter_is_complete = lambda name: True
    run_mod.PAPER_ROOT = ws
    run_mod.PAPER_MODE = mode
    run_mod.run_4stage_with_progress = lambda *a, **k: calls.append(a) or {"route_blocked": "stub"}
    orch.chapter_staleness = staleness
    orch.handoff_is_current = lambda folder: handoff
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            rc = run_mod.run_all_chapters(None, None, None)
    finally:
        (run_mod.outline_chapters_in_order, run_mod.chapter_is_complete,
         run_mod.PAPER_ROOT, run_mod.run_4stage_with_progress, run_mod.PAPER_MODE,
         orch.chapter_staleness, orch.handoff_is_current) = saved
    return buf.getvalue(), rc


# ── codex 审查首轮问题的回归 ─────────────────────────────────────────────
def test_run_all_survey_mode_skips_fingerprint_check():
    """I2:survey 没有阶段指纹记账,已完成章不能被实验模式的检查硬停。"""
    ws = tempfile.mkdtemp()
    os.makedirs(os.path.join(ws, "01-method"))
    calls = []

    def boom(folder):
        raise AssertionError("survey 模式不应调用 chapter_staleness")
    out, rc = _run_all_patched(ws, calls, handoff=False, staleness=boom, mode="survey")
    check("survey 已完成章照常跳过", rc == 0 and "已完成,跳过" in out and not calls, out[-300:])


def test_run_all_reruns_chapter_when_handoff_not_current():
    """I3:交接凭据不符 → 不当作已完成跳过,而是跑该章(只会重跑 Stage 5)。"""
    ws = tempfile.mkdtemp()
    os.makedirs(os.path.join(ws, "01-method"))
    calls = []
    out, _rc = _run_all_patched(ws, calls, handoff=False, staleness=lambda f: [])
    check("凭据不符 → 调用流水线", len(calls) == 1 and "已完成,跳过" not in out, out[-300:])


def test_handoff_is_current_tracks_credential():
    set_idea(IDEA_TEXT)
    folder, outline, _ = make_ws_in_outline(
        "type: method\n\n1. **Framework** (~200 words)\n- 机制", "04-method")
    data = make_data({"test_accuracy": 0.817})
    run_chapter(FullRunAgent(folder), folder, outline, data)
    old = ol.OUTLINE_PATH
    ol.OUTLINE_PATH = Path(outline)
    try:
        check("跑完一章 → 交接凭据有效", orch.handoff_is_current(folder))
        cand = Path(folder, "cross-chapter-draft.md")
        text = cand.read_text(encoding="utf-8")
        cand.unlink()
        check("凭据缺失 → 无效", not orch.handoff_is_current(folder))
        cand.write_text(text, encoding="utf-8")
        manifest = json.loads(Path(folder, ".stage-manifest.json").read_text(encoding="utf-8"))
        saved_entry = dict(manifest["cross-chapter-draft.md"])
        manifest["cross-chapter-draft.md"]["fingerprint"] = "old"
        Path(folder, ".stage-manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        check("凭据指纹不符 → 无效", not orch.handoff_is_current(folder))
        Path(folder, ".stage-manifest.json").write_text(json.dumps(
            {**manifest, "cross-chapter-draft.md": saved_entry}), encoding="utf-8")
        check("恢复凭据 → 有效", orch.handoff_is_current(folder))
        xchap = Path(folder).parent / orch.CROSS_CHAPTER_STATE
        state = xchap.read_text(encoding="utf-8")
        marker = f"- [{Path(folder).name}] "
        tampered = "\n".join((l + " (edited)") if l.strip().startswith(marker) else l
                              for l in state.splitlines()) + "\n"
        xchap.write_text(tampered, encoding="utf-8")
        check("共享状态里本章条目被改 → 无效", not orch.handoff_is_current(folder))
        other = state + "- [99-other] someone else's claim\n"
        xchap.write_text(other, encoding="utf-8")
        check("只有其他章节条目变化 → 仍有效(不触发重跑)", orch.handoff_is_current(folder))
        xchap.write_text(state, encoding="utf-8")
    finally:
        ol.OUTLINE_PATH = old


def test_staleness_catches_new_store_ambiguity():
    """I1:已完成章跳过路径也要拦结果库歧义(宽松加载器下指纹可能不变)。"""
    set_idea(IDEA_TEXT)
    folder, outline, _ = make_ws_in_outline(
        "type: method\n\n1. **Framework** (~200 words)\n- 机制", "05-method")
    data = make_data({"test_accuracy": 0.817})
    run_chapter(FullRunAgent(folder), folder, outline, data)
    check("基线可跳过", staleness(folder, outline, data) == [])
    write(f"{data}/broken.json", "{broken")
    reasons = staleness(folder, outline, data)
    check("新增读不了的文件 → 报陈旧", reasons and "结果库" in reasons[0], str(reasons))


def test_checklist_status_rejects_unknown_ids():
    """I4:最终状态与收敛循环同判据——含清单外 id 的 verdict 不算通过。"""
    d = tempfile.mkdtemp()
    write(f"{d}/review-v1.json", json.dumps({"must_fix": [{"id": "MF1"}]}))
    write(f"{d}/review-verify.round-1.json", json.dumps({"items": [
        {"id": "MF1", "resolved": True}, {"id": "MF9", "resolved": True}]}))
    reasons = orch.checklist_status(d)
    check("未知 id → 未通过并说明", reasons and "MF9" in reasons[0], str(reasons))


class MissingZhAgent(FullRunAgent):
    """定稿漏写 final.zh.md(产物不全 → RES 填充/终稿门禁不会执行)。"""

    def run(self, prompt, **kwargs):
        result = super().run(prompt, **kwargs)
        if "final.md' (English)" in prompt:
            Path(self.folder, "final.zh.md").unlink(missing_ok=True)
        return result


def test_gate_not_run_is_not_verified():
    """I5:门禁没执行 ≠ 通过。"""
    set_idea(IDEA_TEXT)
    folder, outline, _ = make_ws_in_outline(
        "type: method\n\n1. **Framework** (~200 words)\n- 机制", "06-method")
    data = make_data({"test_accuracy": 0.817})
    results, _out = run_chapter(MissingZhAgent(folder), folder, outline, data)
    ver = results.get("verification") or {}
    check("产物不全 → 未核验", ver.get("status") == "unverified", str(ver))
    check("原因写明门禁未执行", any("未执行" in r for r in ver.get("reasons", [])), str(ver))


def test_csv_label_after_identity_column():
    """I6:身份列在首列、标签在后;身份不能唯一确定行时报冲突(不退回行号 key)。"""
    d = tempfile.mkdtemp()
    write(f"{d}/m.csv", "seed,model,accuracy\n1,A,0.8\n1,B,0.9\n2,A,0.81\n")
    store, issues = load_results_store_report(d)
    check("标签在身份列之后仍被识别",
          store.get("m.A.seed=1.accuracy") == 0.8 and store.get("m.B.seed=1.accuracy") == 0.9
          and store.get("m.A.seed=2.accuracy") == 0.81, str(store))
    check("合法多维表无冲突", not issues["duplicates"], str(issues))

    d2 = tempfile.mkdtemp()
    write(f"{d2}/c.csv", "seed,acc\n1,0.8\n1,0.9\n")
    _s2, issues2 = load_results_store_report(d2)
    check("同一身份两个结果 → 报冲突(不被行号 key 掩盖)",
          [k for k, _a, _b in issues2["duplicates"]] == ["c.seed=1.acc"], str(issues2))

    d3 = tempfile.mkdtemp()
    write(f"{d3}/sweep.csv", "seed,lr,acc\n1,0.1,0.80\n1,0.01,0.85\n")
    _s3, issues3 = load_results_store_report(d3)
    check("身份不足以区分的超参扫描 → 报冲突(提示补 run_id)", issues3["duplicates"], str(issues3))
    write(f"{d3}/sweep.csv", "seed,run_id,lr,acc\n1,a,0.1,0.80\n1,b,0.01,0.85\n")
    s4, issues4 = load_results_store_report(d3)
    check("补 run_id 后可加载",
          not issues4["duplicates"] and s4.get("sweep.seed=1.run_id=b.acc") == 0.85, str(s4))


def test_unreadable_subdirectory_is_reported():
    """I7:os.walk 读不了的子目录也要进 unreadable。"""
    if os.geteuid() == 0:
        return   # root 无视权限位,此用例无意义
    d = tempfile.mkdtemp()
    write(f"{d}/ok.json", '{"acc": 0.5}')
    write(f"{d}/locked/r.json", '{"acc": 0.6}')
    os.chmod(f"{d}/locked", 0)
    try:
        _store, issues = load_results_store_report(d)
    finally:
        os.chmod(f"{d}/locked", 0o755)
    check("读不了的子目录被报告", any("locked" in p for p, _e in issues["unreadable"]),
          str(issues))


def test_empty_store_retires_all_ids():
    """Minor 2:数值结果全部消失 → 历史 ID 全部退役,不留在活跃表。"""
    d = tempfile.mkdtemp()
    write(f"{d}/r.json", json.dumps({"a": 1.0, "b": 2.0}))
    _m, path = ensure_res_ids(d)
    os.remove(f"{d}/r.json")
    m2, p2 = ensure_res_ids(d)
    text = path.read_text(encoding="utf-8")
    check("空库返回无目录", m2 == {} and p2 is None)
    check("活跃表清空", parse_res_ids_md(text) == {}, text)
    check("全部转入退役", set(parse_retired_ids(text)) == {"RES-001", "RES-002"}, text)
    write(f"{d}/r.json", json.dumps({"a": 1.0, "c": 3.0}))
    m3, _ = ensure_res_ids(d)
    check("恢复数据后原号恢复、新 key 不复用",
          m3["RES-001"]["key"] == "r.a" and m3.get("RES-003", {}).get("key") == "r.c", str(m3))


# ── 4. 核验状态 + 6. Stage 4 提示词 + 8. skill 注入 ────────────────────
class MismatchFinalAgent(FullRunAgent):
    """定稿里手打一个与结果库矛盾的数字(终稿门禁应抓到)。"""

    def run(self, prompt, **kwargs):
        result = super().run(prompt, **kwargs)
        if "final.md' (English)" in prompt:
            Path(self.folder, "final.md").write_text(
                "# Results\n\nOur model reaches a test accuracy of 91.2% on the benchmark.\n",
                encoding="utf-8")
        return result


def test_final_gate_mismatch_marks_unverified():
    set_idea(IDEA_TEXT)
    folder, outline, _ = make_ws_in_outline(
        "type: results\n\n1. **Results** (~250 words)\n- numbers\n", "03-results")
    data = make_data({"test_accuracy": 0.817})
    results, out = run_chapter(MismatchFinalAgent(folder), folder, outline, data)
    ver = results.get("verification") or {}
    check("产物齐全,跨章交接照常", results.get("stage5_xchap_ok") is True)
    check("状态为未核验", ver.get("status") == "unverified", str(ver))
    check("原因含终稿数字门禁", any("数字门禁" in r for r in ver.get("reasons", [])), str(ver))
    check("不再打印'全部通过'", "核验通过" not in out and "未通过核验" in out, out[-600:])
    vtext = Path(folder, "verification.md").read_text(encoding="utf-8")
    check("verification.md 写明 UNVERIFIED", "Status: UNVERIFIED" in vtext, vtext)
    check("read_verification_status 可读回", orch.read_verification_status(folder) == "unverified")


def test_clean_run_is_verified_and_prompts_are_wired():
    set_idea(IDEA_TEXT)
    folder, outline, _ = make_ws_in_outline(
        "type: method\n\n1. **Framework** (~200 words)\n- 机制", "02-method")
    data = make_data({"test_accuracy": 0.817})
    agent = FullRunAgent(folder)
    results, out = run_chapter(agent, folder, outline, data)
    check("干净一章 → 核验通过",
          (results.get("verification") or {}).get("status") == "verified", str(results.get("verification")))
    check("DONE 行写明核验通过", "核验通过" in out)

    final_prompts = [p for p in agent.prompts if "final.md' (English)" in p]
    check("发出了 Stage 4 定稿提示词", len(final_prompts) == 1, str(len(final_prompts)))
    fp = final_prompts[0]
    check("定稿只读 draft-v2", "Read draft-v2.md, review-v1.md" in fp and "Read draft-v1.md" not in fp)
    check("定稿禁止改数字/占位符", "Do NOT change any claim, number" in fp)

    part_prompts = [p for p in agent.prompts if "Write only Part" in p]
    revise_prompts = [p for p in agent.prompts if "frozen acceptance checklist (JSON)" in p]
    verify_prompts = [p for p in agent.prompts if "VERIFY mode" in p]
    review_prompts = [p for p in agent.prompts if "categorizing issues as MUST FIX" in p]
    check("起草段注入 draft skills",
          part_prompts and all("METHODOLOGY SKILLS" in p and "writing-style.md" in p
                               for p in part_prompts))
    check("修订轮注入 draft skills",
          revise_prompts and all("experiment-writing.md" in p for p in revise_prompts))
    check("评审注入 review rubric",
          review_prompts and all("review-rubric.md" in p for p in review_prompts))
    check("定稿注入 review rubric", "review-rubric.md" in fp)
    check("VERIFY 不注入 skill(只核对清单)",
          verify_prompts and not any("METHODOLOGY SKILLS" in p for p in verify_prompts))
    check("流水线结束后写入作用域已解除", tools._write_scope is None)


# ── 3. 写入作用域 ────────────────────────────────────────────────────────
def _write(path, content="x"):
    return tools.write_file(file_path=str(path), content=content)


def test_write_scope_blocks_author_inputs():
    root = Path(tempfile.mkdtemp())
    chapter = root / "workspace" / "01-method"
    chapter.mkdir(parents=True)
    write(root / "idea.md", "original idea")
    write(root / "data" / "r.json", '{"acc": 0.5}')
    (chapter / "link-out").symlink_to(root / "data")
    buf = io.StringIO()
    tools.set_write_scope(str(chapter))
    try:
        with contextlib.redirect_stdout(buf):
            ok = _write(chapter / "draft-v1.md", "draft")
            sub = _write(chapter / "sub" / "notes.md", "n")
            idea = _write(root / "idea.md", "tampered")
            dotdot = _write(f"{chapter}/../../idea.md", "tampered")
            data = _write(root / "data" / "r.json", '{"acc": 0.99}')
            via_link = _write(chapter / "link-out" / "r.json", '{"acc": 0.99}')
            sibling = _write(root / "workspace" / "01-method-x" / "f.md", "x")
    finally:
        tools.set_write_scope(None)
    check("本章目录内可写", ok.startswith("Successfully") and sub.startswith("Successfully"))
    for label, res in (("idea.md", idea), ("../ 绕过", dotdot), ("data/", data),
                       ("符号链接绕过", via_link), ("前缀相同的兄弟目录", sibling)):
        check(f"拒绝写 {label}", res.startswith("Error: write refused"), res)
    check("idea.md 内容未被改", (root / "idea.md").read_text(encoding="utf-8") == "original idea")
    check("data 内容未被改", (root / "data" / "r.json").read_text(encoding="utf-8") == '{"acc": 0.5}')
    check("拒绝写入有日志", "DENIED" in buf.getvalue())
    check("解除作用域后不受限", _write(root / "outline.md", "o").startswith("Successfully"))


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in tests:
        fn()
        print(f"  ok  {fn.__name__}")
    print(f"\nDRAFT RELIABILITY TESTS PASSED — {len(PASSED)} 项检查全过")
