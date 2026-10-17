"""第 0 期漏洞修复的离线回归(不调 API)。

1 写入隔离:Agent 不能导入 os;独立阶段 write_file 受文件白名单约束
2 引用 key 白名单:未知 key 不插入;终稿里的未知 \\cite key 进核验原因
3 数字缺口不被洗白:⟨未知结果⟩ / 裸 RES 编号每次核验都重扫(含第二次续跑)
4 前置阶段(备料/证据挖掘)失败即停;文献表读取不再被静默吞掉
5 中文大纲翻译结果结构校验
6 read_file 上限、read_file_range、list_folder 上限

运行: python tests/test_phase0_fixes.py
"""
import contextlib
import io
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

import agents.agents as agents_mod
import agents.orchestrator as orch
import agents.outline as ol
import agents.tools as tools
from agents.citation_supplement import (apply_insertions, citable_keys,
                                        unknown_cite_keys)
from agents.outline import parse_outline
from agents.outline_expand import validate_translation
from agents.result_ids import (merge_res_ids_full, parse_res_ids_md,
                               render_res_ids_md, residual_res_problems)
from test_pipeline_routing import (FullRunAgent, IDEA_TEXT, RecordingAgent,
                                   make_data, make_ws_in_outline, set_idea)

PASSED = []


def check(label, cond, detail=""):
    if not cond:
        raise AssertionError(f"{label}: {detail or 'failed'}")
    PASSED.append(label)


def write(path, text):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(text, encoding="utf-8")


def run_chapter(agent, folder, outline, data_root, manager=None):
    old = ol.OUTLINE_PATH
    ol.OUTLINE_PATH = Path(outline)
    buf = io.StringIO()
    try:
        orch.DATA_ROOT = data_root
        with contextlib.redirect_stdout(buf):
            results = orch.run_4stage_with_progress(agent, agent, folder, manager)
    finally:
        ol.OUTLINE_PATH = old
    return results, buf.getvalue()


# ── 1 写入隔离 ──────────────────────────────────────────────────────────
def test_no_os_import_and_allowlist():
    check("Agent 不允许导入 os", "os" not in agents_mod.AUTHORIZED_IMPORTS,
          str(agents_mod.AUTHORIZED_IMPORTS))
    d = tempfile.mkdtemp()
    allowed, other = Path(d, "ok.md"), Path(d, "idea.md")
    tools.set_write_allowlist([allowed])
    try:
        r1 = tools.write_file(str(allowed), "x")
        r2 = tools.write_file(str(other), "y")
    finally:
        tools.set_write_allowlist(None)
    check("白名单内可写", allowed.is_file() and "Successfully" in r1, r1)
    check("白名单外拒绝", not other.exists() and "refused" in r2, r2)
    r3 = tools.write_file(str(other), "z")
    check("解除白名单后恢复", other.is_file(), r3)


# ── 2 引用 key 白名单 ───────────────────────────────────────────────────
def test_citation_whitelist():
    ins = [{"sentence": "Prior work shows large gains across many tasks in practice.",
            "suggested_key_or_url": "REF-9999", "source": "bib"},
           {"sentence": "Another established claim about transformers in vision tasks.",
            "suggested_key_or_url": "REF-0001", "source": "bib"}]
    text = " ".join(i["sentence"] for i in ins)
    new, applied, skipped = apply_insertions(text, ins, allowed_keys={"REF-0001"})
    check("合法 key 插入", "\\cite{REF-0001}" in new and applied == 1, new)
    check("虚构 key 不插入并交人工",
          "REF-9999" not in new and any("REF-9999" in s["suggested_key_or_url"] for s in skipped), str(skipped))
    check("未提供白名单时保持旧行为(向后兼容)",
          apply_insertions(text, ins)[1] == 2)
    bad = unknown_cite_keys(r"a \cite{REF-0001, REF-7} b \citep[see][p.2]{x1} c \citet{REF-0001}",
                            {"REF-0001"})
    check("多 key / 可选参数 / citet 都能扫到未知 key", bad == ["REF-7", "x1"], str(bad))
    check("citable_keys 来自本地库(返回 set)", isinstance(citable_keys(), set))


class UnknownCiteFinalAgent(FullRunAgent):
    def run(self, prompt, **kwargs):
        result = super().run(prompt, **kwargs)
        if "final.md' (English)" in prompt:
            Path(self.folder, "final.md").write_text(
                "# Method\n\nWe build on prior art \\cite{REF-DOES-NOT-EXIST}.\n", encoding="utf-8")
        return result


def test_unknown_cite_in_final_marks_unverified():
    set_idea(IDEA_TEXT)
    folder, outline, _ = make_ws_in_outline(
        "type: method\n\n1. **Framework** (~200 words)\n- 机制", "02-method")
    results, _out = run_chapter(UnknownCiteFinalAgent(folder), folder, outline, make_data({"acc": 0.8}))
    ver = results.get("verification") or {}
    check("终稿含未知引用 key → 未核验", ver.get("status") == "unverified", str(ver))
    check("原因点名该 key", any("REF-DOES-NOT-EXIST" in r for r in ver.get("reasons", [])), str(ver))
    check("记入 todo.md", "REF-DOES-NOT-EXIST" in Path(folder, "todo.md").read_text(encoding="utf-8"))


# ── 3 数字缺口不被洗白 ──────────────────────────────────────────────────
def test_residual_res_scan_and_id_formats():
    probs = residual_res_problems("a ⟨未知结果 RES-009⟩ b {{RES-001}} c RES-002 d")
    check("扫出三类残留", len(probs) == 3, str(probs))
    check("干净文本无残留", residual_res_problems("plain text 82.3%") == [])
    m, r = merge_res_ids_full({"a.x": 1.0, "b.y": 2.5}, "")
    new_fmt = render_res_ids_md(m, r)
    check("ID 列带花括号可直接复制", "| {{RES-001}} |" in new_fmt, new_fmt)
    old_fmt = new_fmt.replace("{{RES-001}}", "RES-001").replace("{{RES-002}}", "RES-002")
    check("新旧格式都能解析(已有目录不被判损坏)",
          parse_res_ids_md(new_fmt) == parse_res_ids_md(old_fmt) == {"RES-001": "a.x", "RES-002": "b.y"})
    m2, _ = merge_res_ids_full({"a.x": 1.0, "b.y": 2.5, "c.z": 3.0}, old_fmt)
    check("旧格式目录合并后编号不漂移", m2["RES-001"]["key"] == "a.x" and m2["RES-003"]["key"] == "c.z")


class UnknownResFinalAgent(FullRunAgent):
    def run(self, prompt, **kwargs):
        result = super().run(prompt, **kwargs)
        if "final.md' (English)" in prompt:
            Path(self.folder, "final.md").write_text(
                "# Results\n\nThe accuracy is {{RES-999}}.\n", encoding="utf-8")
        return result


def test_unknown_res_survives_second_run():
    set_idea(IDEA_TEXT)
    folder, outline, _ = make_ws_in_outline(
        "type: method\n\n1. **Framework** (~200 words)\n- 机制", "02-method")
    data = make_data({"acc": 0.8})
    r1, _ = run_chapter(UnknownResFinalAgent(folder), folder, outline, data)
    check("首次:缺口 → 未核验",
          (r1.get("verification") or {}).get("status") == "unverified", str(r1.get("verification")))
    check("首次已把占位符替换成未知标记",
          "⟨未知结果 RES-999⟩" in Path(folder, "final.md").read_text(encoding="utf-8"))
    r2, _ = run_chapter(UnknownResFinalAgent(folder), folder, outline, data)
    ver2 = r2.get("verification") or {}
    check("第二次续跑:缺口仍在 → 仍未核验(不被洗白)", ver2.get("status") == "unverified", str(ver2))
    check("第二次续跑原因点名 RES-999", any("RES-999" in r for r in ver2.get("reasons", [])), str(ver2))


# ── 4 前置阶段失败即停 ─────────────────────────────────────────────────
class NoopAgent:
    _offline_test_stub = True

    def __init__(self):
        self.prompts = []

    def run(self, prompt, **kwargs):
        self.prompts.append(prompt)
        return "did nothing"


def test_stage0_failure_stops_chapter():
    import config
    set_idea(IDEA_TEXT)
    idx = Path(tempfile.mkdtemp(), "ref-index.md")
    idx.write_text("| he2016resnet | T | 2016 | V | | s | 有 | 有 |\n", encoding="utf-8")
    old = config.REF_INDEX_PATH
    config.REF_INDEX_PATH = idx
    try:
        folder, outline, _ = make_ws_in_outline(
            "type: method\n\n1. **Framework** (~200 words)\n- 机制", "02-method")
        os.remove(os.path.join(folder, "input.md"))
        draft = RecordingAgent(folder)
        results, out = run_chapter(draft, folder, outline, make_data({"acc": 0.8}), manager=NoopAgent())
        check("备料没产出 → 本章停止", "input.md 没有生成" in out, out[-400:])
        check("没有进入起草", not any("draft-v1.part" in p for p in draft.prompts))
        check("没有产出 draft-v1.md", not Path(folder, "draft-v1.md").exists())

        folder2, outline2, _ = make_ws_in_outline(
            "type: method\n\n1. **Framework** (~200 words)\n- 机制", "02-method")
        results2, out2 = run_chapter(NoopAgent(), folder2, outline2, make_data({"acc": 0.8}), manager=NoopAgent())
        check("证据挖掘没产出 → 本章停止", "evidence-pack.md 没有生成" in out2, out2[-400:])

        # 文献索引缺失:备料抛错,同样停章(不是静默当成空文献继续)
        config.REF_INDEX_PATH = Path(tempfile.mkdtemp(), "missing.md")
        folder3, outline3, _ = make_ws_in_outline(
            "type: method\n\n1. **Framework** (~200 words)\n- 机制", "02-method")
        os.remove(os.path.join(folder3, "input.md"))
        r3, out3 = run_chapter(RecordingAgent(folder3), folder3, outline3, make_data({"acc": 0.8}), manager=NoopAgent())
        check("文献索引缺失 → 备料出错并停章", "Stage 0 备料出错" in out3, out3[-400:])
    finally:
        config.REF_INDEX_PATH = old


def test_input_material_uses_reference_index():
    """备料读文献索引(不再读已删除的 bibliography.md);索引缺失是错误,不是空文献。"""
    import config
    from agents.evidence_mining import run_input_material
    d = tempfile.mkdtemp()
    seen = {}

    def fake_stage(agent, name, prompt):
        seen["prompt"] = prompt

    idx = Path(d, "ref-index.md")
    idx.write_text("| he2016resnet | Deep Residual Learning | 2016 | CVPR | | 残差连接 | 有 | 有 |\n", encoding="utf-8")
    old = config.REF_INDEX_PATH
    try:
        config.REF_INDEX_PATH = idx
        run_input_material(object(), d, "02-method", "idea", fake_stage, lambda n: None, lambda ok: True)
        check("备料提示词带上了文献索引内容", "he2016resnet" in seen.get("prompt", ""), seen.get("prompt", "")[-300:])
        config.REF_INDEX_PATH = Path(d, "missing.md")
        raised = None
        try:
            run_input_material(object(), tempfile.mkdtemp(), "02-method", "idea", fake_stage,
                               lambda n: None, lambda ok: True)
        except FileNotFoundError as exc:
            raised = exc
        check("文献索引缺失 → 报错而不是当成空文献", raised is not None)
    finally:
        config.REF_INDEX_PATH = old


# ── 5 翻译结果结构校验 ─────────────────────────────────────────────────
def _outline(body):
    p = Path(tempfile.mkdtemp()) / "o.md"
    p.write_text(body, encoding="utf-8")
    return p


def test_translation_validation():
    zh = parse_outline(_outline(
        "## 1. 摘要\ntype: abstract\n- 一句问题\n\n"
        "## 2. 方法\ntype: method\n### 2.1 总体 (~300 words)\n- 要点一\n- 要点二\n"))
    en_ok = parse_outline(_outline(
        "## 1. Abstract\ntype: abstract\n- one line\n\n"
        "## 2. Method\ntype: method\n### 2.1 Overview (~300 words)\n- point one\n- point two\n"))
    check("结构一致 → 通过", validate_translation(zh, en_ok) == [], str(validate_translation(zh, en_ok)))
    en_missing = parse_outline(_outline("## 1. Abstract\ntype: abstract\n- one line\n"))
    check("漏章 → 拒绝", any("章数" in p for p in validate_translation(zh, en_missing)))
    en_type = parse_outline(_outline(
        "## 1. Abstract\ntype: abstract\n- one line\n\n"
        "## 2. Method\ntype: theory\n### 2.1 Overview (~300 words)\n- a\n- b\n"))
    check("type 被改 → 拒绝", any("type" in p for p in validate_translation(zh, en_type)))
    en_words = parse_outline(_outline(
        "## 1. Abstract\ntype: abstract\n- one line\n\n"
        "## 2. Method\ntype: method\n### 2.1 Overview (~500 words)\n- a\n- b\n"))
    check("字数被改 → 拒绝", any("字数" in p for p in validate_translation(zh, en_words)))
    en_bullets = parse_outline(_outline(
        "## 1. Abstract\ntype: abstract\n- one line\n\n"
        "## 2. Method\ntype: method\n### 2.1 Overview (~300 words)\n- a\n"))
    check("要点被合并/拆分是合法翻译(不拒绝)", validate_translation(zh, en_bullets) == [])


# ── 6 读取护栏 ─────────────────────────────────────────────────────────
def test_read_guards():
    d = tempfile.mkdtemp()
    big = Path(d, "big.log")
    big.write_text("\n".join(f"line {i}" for i in range(1, 20001)), encoding="utf-8")
    out = tools.read_file(str(big))
    check("read_file 超长被截断并给出提示",
          len(out) < tools.MAX_READ_CHARS + 400 and "TRUNCATED" in out and "read_file_range" in out)
    small = Path(d, "s.md")
    small.write_text("hello\nworld\n", encoding="utf-8")
    check("小文件原样返回", tools.read_file(str(small)) == "hello\nworld\n")
    rng = tools.read_file_range(str(big), 100, 3)
    check("按行范围读取", "line 100" in rng and "line 102" in rng and "line 103" not in rng
          and "of 20000" in rng, rng)
    check("范围条数有上限", tools.read_file_range(str(big), 1, 99999).count("\nline ") <= tools.MAX_RANGE_LINES)
    check("越界范围有明确提示", "no lines in range" in tools.read_file_range(str(big), 99999, 5))
    many = Path(d, "many")
    many.mkdir()
    for i in range(tools.MAX_LIST_ENTRIES + 30):
        Path(many, f"f{i:04d}.txt").write_text("x")
    listing = tools.list_folder(str(many))
    check("list_folder 超限截断并提示", "TRUNCATED" in listing
          and listing.count("\n") <= tools.MAX_LIST_ENTRIES + 1, listing[-120:])
    check("三个 Agent 工具集含 read_file_range",
          "read_file_range" in Path(agents_mod.__file__).read_text(encoding="utf-8"))


def test_program_owned_files_and_handedit_reverify():
    d = tempfile.mkdtemp()
    tools.set_write_scope(d)
    try:
        r1 = tools.write_file(os.path.join(d, "verification.md"), "x")
        r2 = tools.write_file(os.path.join(d, ".stage-manifest.json"), "{}")
        r3 = tools.write_file(os.path.join(d, "draft-v1.md"), "ok")
    finally:
        tools.set_write_scope(None)
    check("程序专属文件 Agent 不能写", "refused" in r1 and "refused" in r2 and Path(d, "draft-v1.md").is_file(), r1 + r2)
    # 手改终稿后,旧的 MUST FIX 验收不再被信任
    set_idea(IDEA_TEXT)
    folder, outline, _ = make_ws_in_outline("type: method\n\n1. **Framework** (~200 words)\n- 机制", "02-method")
    data = make_data({"acc": 0.8})
    res, _o = run_chapter(FullRunAgent(folder), folder, outline, data)
    check("首次干净", (res.get("verification") or {}).get("status") == "verified", str(res.get("verification")))
    Path(folder, "final.md").write_text("# Method\n\nHand edited text.\n", encoding="utf-8")
    old = ol.OUTLINE_PATH
    ol.OUTLINE_PATH = Path(outline)
    try:
        orch.DATA_ROOT = data
        with contextlib.redirect_stdout(io.StringIO()):
            v = orch.reverify_chapter(folder)
    finally:
        ol.OUTLINE_PATH = old
    check("手改终稿后重新核验 → 未核验并说明", v["status"] == "unverified"
          and any("手改" in r for r in v["reasons"]), str(v))


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print(f"\nPHASE0 FIXES TESTS PASSED — {len(PASSED)} 项检查全过")
