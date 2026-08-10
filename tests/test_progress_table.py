"""进度表格渲染的离线测试(不调 API)。

验证 orchestrator 新增的 `render_stage_table` / `_print_stage_table`:
- 能根据 results dict + 工作区产物推断各 Stage 状态(done / skipped / failed / 未执行)
- 输出是 Markdown 表格,含所有 Stage 行与产物列
- 跑完整流水线(假 Agent)后表格能正常渲染,不抛错

Run: python tests/test_progress_table.py
"""
import io
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import config
import agents.orchestrator as orch
from agents.orchestrator import render_stage_table, _print_stage_table

# 复用 test_pipeline_routing 的假 Agent 与工作区构造 helper
from test_pipeline_routing import (
    IDEA_TEXT, RecordingAgent, make_ws_in_outline, set_idea, run_pipeline_in_outline,
)

PASSED = []


class FullRecordingAgent:
    """跑完整条流水线用的假 Agent:按 prompt 里出现的产物名写出对应文件,
    覆盖所有 Stage 的 verify() 检查(含 review/final)。"""

    def __init__(self, folder: str):
        self.folder = folder
        self.prompts = []

    def run(self, prompt, **kwargs):
        self.prompts.append(prompt)
        # 所有会被 verify() 检查的产物名
        for name in ("evidence-pack.md", "draft-v1.plan.md", "todo.md",
                     "draft-v1.part-1.md", "draft-v1.part-2.md", "draft-v1.part-3.md",
                     "draft-v1.md", "review-v1.md", "review-v1.json",
                     "draft-v2.md", "final.md", "final.zh.md", "decision.md",
                     "cross-chapter-draft.md"):
            if f"/{name}'" in prompt and not os.path.exists(os.path.join(self.folder, name)):
                path = Path(self.folder, name)
                if name == "review-v1.json":
                    # 收敛循环要 read_json_artifact 解析它:必须是合法 JSON,
                    # 含 must_fix 空列表(无 MUST FIX → 单轮 polish)。
                    path.write_text(
                        '{"scores": {"overall": 5}, "decision": "ACCEPT", '
                        '"must_fix": [], "should_fix": [], "consider": []}',
                        encoding="utf-8")
                else:
                    path.write_text(
                        f"# {name}\n\nplaceholder written by the test harness.\n",
                        encoding="utf-8")
        return f"wrote artifacts for prompt #{len(self.prompts)}"

    def text(self) -> str:
        return "\n\n".join(self.prompts)


def check(label: str, condition: bool, detail: str = "") -> None:
    if not condition:
        raise AssertionError(f"{label}: {detail or 'failed'}")
    PASSED.append(label)


def test_render_stage_table_all_done():
    """所有 Stage 产物齐全时,表格全部标 ✓ 完成。"""
    results = {
        "stage0_evidence": "ok",
        "stage1_plan": "ok",
        "stage1_parts": ["ok"],
        "stage2": "ok",
        "stage3": "ok",
        "stage4": "ok",
        "stage5_xchap_ok": True,
    }
    folder = Path(tempfile.mkdtemp())
    for name in ("evidence-pack.md", "draft-v1.plan.md", "draft-v1.md",
                 "review-v1.json", "review-v1.md", "draft-v2.md",
                 "final.md", "final.zh.md"):
        (folder / name).write_text("# x\n", encoding="utf-8")

    table = render_stage_table(str(folder), results, running_stage=-1)
    check("输出含表头", "阶段" in table and "状态" in table and "产物" in table)
    check("含全部 Stage 行", all(f"| {label}" in table for label in
          ["0 证据挖掘", "1a 规划", "1b 起草", "2 评审", "3 收敛修订", "4 定稿", "5 跨章交接"]))
    check("全部完成", table.count("完成") == 7, f"got {table.count('完成')}")
    check("产物列列出 final.md", "final.md" in table)


def test_render_stage_table_skipped():
    """部分 Stage 被断点续跑跳过时,标 ⏭ 跳过。"""
    results = {
        "stage0_evidence": "skipped",
        "stage1_plan": "skipped",
        "stage2": "skipped",
        "stage3": "skipped",
        "stage4": "skipped",
        "stage5_xchap_ok": True,
    }
    folder = Path(tempfile.mkdtemp())
    for name in ("evidence-pack.md", "draft-v1.plan.md", "draft-v1.md",
                 "review-v1.json", "final.md"):
        (folder / name).write_text("# x\n", encoding="utf-8")
    table = render_stage_table(str(folder), results, running_stage=-1)
    check("跳过标出现", "跳过" in table)
    check("skipped 状态出现", "跳过" in table)


def test_render_stage_table_failed_agent():
    """agent 调用失败(error dict)时,对应 Stage 标 ✗ 失败。"""
    results = {
        "stage0_evidence": "ok",
        "stage1_plan": {"error": "model call failed", "exception_type": "Timeout"},
        "stage5_xchap_ok": False,
    }
    folder = Path(tempfile.mkdtemp())
    for name in ("evidence-pack.md",):
        (folder / name).write_text("# x\n", encoding="utf-8")
    table = render_stage_table(str(folder), results, running_stage=-1)
    check("stage1 失败", "失败" in table, table)


def test_render_stage_table_route_blocked():
    """路由硬停时,从阻塞点起标未执行。"""
    results = {"route_blocked": "not in outline"}
    folder = Path(tempfile.mkdtemp())
    table = render_stage_table(str(folder), results, running_stage=-1)
    check("路由阻塞显示未执行", "未执行" in table, table)


def test_print_stage_table_writes_without_error():
    """_print_stage_table 打印不抛错,输出包含表格。"""
    results = {
        "stage0_evidence": "ok",
        "stage1_plan": "ok",
        "stage2": "ok",
        "stage3": "ok",
        "stage4": "ok",
        "stage5_xchap_ok": True,
    }
    folder = Path(tempfile.mkdtemp())
    for name in ("evidence-pack.md", "draft-v1.plan.md", "draft-v1.md",
                 "review-v1.json", "final.md"):
        (folder / name).write_text("# x\n", encoding="utf-8")
    buf = io.StringIO()
    old = sys.stdout
    sys.stdout = buf
    try:
        _print_stage_table(str(folder), results)
    finally:
        sys.stdout = old
    out = buf.getvalue()
    check("输出含表格边框", "章节进度" in out, out[:200])


def test_pipeline_end_to_end_render():
    """FullRecordingAgent 跑完整条流水线,末尾表格能渲染,不抛错。"""
    set_idea(IDEA_TEXT)
    folder, outline, _ = make_ws_in_outline(
        "type: method\n\n1. **Method** (~300 words)\n- x\n", "02-method")
    data_root = tempfile.mkdtemp()
    buf = io.StringIO()
    old_stdout, old_sys = sys.stdout, os.sys.stdout
    sys.stdout = buf
    os.sys.stdout = buf
    try:
        import agents.outline as ol
        original = ol.OUTLINE_PATH
        ol.OUTLINE_PATH = Path(outline)
        try:
            agent = FullRecordingAgent(folder)
            orch.DATA_ROOT = data_root
            results = orch.run_4stage_with_progress(agent, agent, folder, None)
        finally:
            ol.OUTLINE_PATH = original
    finally:
        sys.stdout, os.sys.stdout = old_stdout, old_sys
    out = buf.getvalue()
    # 完整跑完应有 DONE 与表格
    check("流水线跑完", "DONE" in out or "draft-v1" in out, out[:300])
    check("末尾出现进度表格", "章节进度" in out, out[-800:])
    check("results 含 stage4", "stage4" in results, str(list(results.keys())))


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for test in tests:
        test()
    print(f"{len(PASSED)} checks passed across {len(tests)} tests:")
    for label in PASSED:
        # Windows GBK 终端打不出 emoji(⏭ 等),打印 ASCII 化版本避免 UnicodeEncodeError
        safe = label.encode("ascii", "replace").decode("ascii")
        print(f"  ok  {safe}")
    print("\nPROGRESS TABLE TESTS PASSED")
