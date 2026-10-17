"""本次 4 项优化的离线验证(不调 API、不装 smolagents)。

smolagents 未安装时用一个最小 stub 顶掉:被测的都是纯 Python 逻辑,
CodeAgent / Model / @tool 只在 import 期被引用。

运行: python tests/test_optimizations.py
"""
import re
import sys
import tempfile
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# ── stub smolagents(仅在真包缺失时启用) ──────────────────────────────
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
from agents.chapter_type import IDEA, DATA, MIXED, BLOCKING, ADVISORY, OFF

PASSED = []


def check(label: str, cond: bool, detail: str = "") -> None:
    if not cond:
        raise AssertionError(f"{label}: {detail or 'failed'}")
    PASSED.append(label)


def sections(*titles) -> list[dict]:
    return [{"number": i, "title": t, "target_words": 300}
            for i, t in enumerate(titles, start=1)]


# ── 优化 1:Alignment 硬编码特例已删除 ────────────────────────────────
def test_no_alignment_special_case():
    """确认特例分支真的没了,而不只是行为看起来对。

    用 AST 取函数体的可执行部分:注释和 docstring 里会提到 rlvr/constitutional
    来解释这段历史,按文本 grep 会误报。
    """
    import ast
    import inspect
    tree = ast.parse(inspect.getsource(orch.build_stage1_parts))
    fn = tree.body[0]
    body = fn.body[1:] if (fn.body and isinstance(fn.body[0], ast.Expr)
                           and isinstance(fn.body[0].value, ast.Constant)
                           and isinstance(fn.body[0].value.value, str)) else fn.body
    code = "\n".join(ast.unparse(node) for node in body)
    check("build_stage1_parts 里没有 is_alignment 判断", "is_alignment" not in code, code[:400])
    check("build_stage1_parts 里没有按标题的关键词特例",
          "rlvr" not in code.lower() and "constitutional" not in code.lower(), code[:400])
    check("build_stage1_parts 不再读小节标题做判断", "title" not in code.split("titles =")[0],
          code[:400])

    # 命中旧特例的小节命名,现在必须走通用 2/2/2 分组。
    alignment_like = sections("RLVR", "Constitutional AI", "RLHF",
                              "DPO", "Process Reward", "Outlook")
    parts = orch.build_stage1_parts(alignment_like)
    check("6 节走通用 2/2/2 分组",
          [p["numbers"] for p in parts] == [[1, 2], [3, 4], [5, 6]],
          str([p["numbers"] for p in parts]))

    # 段数随小节数自适应:1/2 个小节不再硬凑三段,
    # 否则 Abstract 章会多出两个没有来源的空段、各带 700 词目标。
    for count, want in {
        0: [[]],                                    # brief 没解析出小节 → 单段兜底
        1: [[1]],
        2: [[1], [2]],
        3: [[1], [2], [3]],
        4: [[1, 2], [3, 4], []],
        5: [[1, 2], [3, 4], [5]],
        7: [[1, 2], [3, 4], [5, 6, 7]],
    }.items():
        got = [p["numbers"] for p in
               orch.build_stage1_parts(sections(*(f"s{i}" for i in range(1, count + 1))))]
        check(f"{count} 节分组边界", got == want, f"got {got}, want {want}")



# ── 优化 3:brief 指纹捕获路由变化 + 陈旧产物告警 ─────────────────────
# context-pack 已删除,路由变化改由 brief.md 首行的 outline 指纹(chapter_fingerprint,
# 覆盖 type + 小节级 type + 要点)统一捕获。pack_fingerprint/stamp_pack_fingerprint/
#read_pack_fingerprint 三件套随之移除。这里验陈旧产物检测本身仍然成立(告警函数保留,
#触发源改成 brief 指纹)。
def test_chapter_fingerprint_captures_route():
    from agents.outline import chapter_fingerprint
    method = {"type": "method",
              "sections": [{"number": 1, "title": "Framework", "target_words": 200,
                            "bullets": ["design"], "type": None}]}
    results = {"type": "results",
               "sections": [{"number": 1, "title": "Main", "target_words": 200,
                             "bullets": ["numbers"], "type": None}]}
    fp_method = chapter_fingerprint(method)
    fp_results = chapter_fingerprint(results)
    check("不同路由(type 不同)产生不同指纹", fp_method != fp_results,
          f"{fp_method} == {fp_results}")
    check("同一路由指纹稳定", chapter_fingerprint(dict(method)) == fp_method)
    check("method 指纹含 type=method", "type=method" in fp_method, fp_method)
    # 小节级 type 也进指纹(分段路由靠它)。
    a = {"type": "method",
         "sections": [{"number": 1, "title": "S1", "target_words": 200,
                       "bullets": [], "type": "method"},
                      {"number": 2, "title": "S2", "target_words": 200,
                       "bullets": [], "type": "results"}]}
    b = {"type": "method",
         "sections": [{"number": 1, "title": "S1", "target_words": 200,
                       "bullets": [], "type": "method"},
                      {"number": 2, "title": "S2", "target_words": 200,
                       "bullets": [], "type": "discussion"}]}
    check("小节级 type 变化会改变指纹", chapter_fingerprint(a) != chapter_fingerprint(b))


def test_pack_fingerprint_helpers_removed():
    # context-pack 删除后,这三件套不应再存在(测试 import 它们的旧代码会 AttributeError)。
    check("pack_fingerprint 已移除", not hasattr(orch, "pack_fingerprint"))
    check("stamp_pack_fingerprint 已移除", not hasattr(orch, "stamp_pack_fingerprint"))
    check("read_pack_fingerprint 已移除", not hasattr(orch, "read_pack_fingerprint"))


def test_stale_artifact_warning():
    folder = tempfile.mkdtemp()
    # 静态清单里的产物
    for name in ("evidence-pack.md", "draft-v1.plan.md", "review-v1.json"):
        Path(folder, name).write_text("x", encoding="utf-8")
    # glob 产物(收敛循环轮次文件)——修复前清单里没有它们
    for name in ("draft-v2.round-1.md", "review-verify.round-1.json",
                 "draft-v2.round-2.md"):
        Path(folder, name).write_text("x", encoding="utf-8")

    stale = orch.warn_stale_route_artifacts(folder, "ws/chapter", "old", "new")
    stale_set = set(stale)
    check("静态清单产物被列出",
          {"evidence-pack.md", "draft-v1.plan.md", "review-v1.json"} <= stale_set,
          str(stale_set))
    check("收敛轮次 round-1 draft 被列出",
          "draft-v2.round-1.md" in stale_set, str(stale_set))
    check("收敛轮次 round-1 verdict 被列出",
          "review-verify.round-1.json" in stale_set, str(stale_set))
    check("收敛轮次 round-2 draft 被列出",
          "draft-v2.round-2.md" in stale_set, str(stale_set))
    check("todo.md 不算陈旧产物(可能含人工内容)",
          "todo.md" not in orch.ROUTE_DEPENDENT_ARTIFACTS)
    check("报警不删除文件", Path(folder, "evidence-pack.md").exists())
    check("干净目录下无陈旧项",
          orch.warn_stale_route_artifacts(tempfile.mkdtemp(), "ws/x", "old", "new") == [])


def test_all_three_agents_get_search_literature():
    """三个 Agent 的 tools 列表都必须含 search_literature。

    审查指出原测试的问题:用 except Exception 兜住断言失败,再退回"源码里出现过
    search_literature"这个宽松检查——而它只需要出现一次就通过,所以 Review 和
    Manager 漏接线时测试依然是绿的。这里换成替换 CodeAgent 捕获三个 tools 列表,
    断言失败就是失败,没有 fallback。
    """
    import agents.agents as ag_mod

    class CaptureAgent:
        def __init__(self, *args, **kwargs):
            self.tools = kwargs.get("tools", [])

    original = ag_mod.CodeAgent
    try:
        ag_mod.CodeAgent = CaptureAgent
        manager, draft, review = ag_mod.create_agents(object(), object(), object())
    finally:
        ag_mod.CodeAgent = original

    for label, agent in (("manager", manager), ("draft", draft), ("review", review)):
        names = {getattr(t, "name", getattr(t, "__name__", "")) for t in agent.tools}
        check(f"{label}_agent 持有文献受控读取工具",
              {"read_reference", "search_reference_text"} <= names, str(names))
        check(f"{label}_agent 持有读取护栏工具且原有工具未丢",
              {"read_file", "read_file_range", "write_file", "list_folder"} <= names, str(names))
        check(f"{label}_agent 不再有联网/旧索引检索工具",
              not ({"search_literature", "search_references"} & names), str(names))


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items())
             if k.startswith("test_") and callable(v)]
    for test in tests:
        test()
    print(f"\n{len(PASSED)} 项检查通过 / {len(tests)} 个测试:")
    for label in PASSED:
        print(f"  ok  {label}")
    print("\nOPTIMIZATION TESTS PASSED")
