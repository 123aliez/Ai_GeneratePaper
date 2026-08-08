"""本次 4 项优化的离线验证(不调 API、不装 smolagents)。

smolagents 未安装时用一个最小 stub 顶掉:被测的都是纯 Python 逻辑,
CodeAgent / LiteLLMModel / @tool 只在 import 期被引用。

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
    stub.LiteLLMModel = object
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

    # 段数随小节数自适应(逐章模式引入):1/2 个小节不再硬凑三段,
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


# ── 优化 2:survey 专用路径在 experiment 模式下拒绝执行 ────────────────
def test_survey_paths_refuse_in_experiment_mode():
    check("当前是 experiment 模式", orch.PAPER_MODE == "experiment", orch.PAPER_MODE)
    for name, call in [
        ("build_4stage_manager_prompt", lambda: orch.build_4stage_manager_prompt("x")),
        ("run_4stage_via_manager", lambda: orch.run_4stage_via_manager(None, "x")),
        ("run_4stage_via_manager_stream", lambda: orch.run_4stage_via_manager_stream(None, "x")),
        ("run_4stage_direct", lambda: orch.run_4stage_direct(None, None, "x")),
    ]:
        try:
            call()
        except RuntimeError as exc:
            check(f"{name} 在 experiment 模式下抛 RuntimeError",
                  "survey" in str(exc) and "experiment" in str(exc), str(exc))
        else:
            raise AssertionError(f"{name} 应当拒绝执行,却正常返回了")


# ── 优化 3:context-pack 路由指纹 ─────────────────────────────────────
def test_pack_fingerprint():
    method = {"type": "method", "family": IDEA, "gate": ADVISORY, "section_types": {1: "method"}}
    results = {"type": "results", "family": DATA, "gate": BLOCKING, "section_types": {1: "results"}}

    fp_method, fp_results = orch.pack_fingerprint(method), orch.pack_fingerprint(results)
    check("不同路由产生不同指纹", fp_method != fp_results, f"{fp_method} == {fp_results}")
    check("同一路由指纹稳定", orch.pack_fingerprint(dict(method)) == fp_method)
    check("指纹含 family 与 gate",
          "family=idea" in fp_method and "gate=advisory" in fp_method, fp_method)

    # 只改一个小节的 type 也必须换指纹(整篇 brief 的分段路由靠它)。
    a = {"type": "method", "family": MIXED, "gate": ADVISORY,
         "section_types": {1: "method", 2: "results"}}
    b = {"type": "method", "family": MIXED, "gate": ADVISORY,
         "section_types": {1: "method", 2: "discussion"}}
    check("小节级 type 变化会改变指纹",
          orch.pack_fingerprint(a) != orch.pack_fingerprint(b))

    # 写作路由也必须进指纹:手建单章后来补进 outline.md,证据路由一个字没变,但
    # 写作契约从"自包含"翻成"整篇第 N 章",而 plan / 各 part / review 全是按自
    # 包含写的,断点续跑会原样复用它们。
    check("FULL 与 SINGLE 产生不同的断点续跑指纹",
          orch.pack_fingerprint(method, orch.FULL)
          != orch.pack_fingerprint(method, orch.SINGLE))
    check("不传 write_mode 时指纹与旧版一致(向后兼容)",
          orch.pack_fingerprint(method) == fp_method)
    check("write_mode 出现在指纹串里",
          "write_mode=full" in orch.pack_fingerprint(method, orch.FULL))

    # 盖章 → 读回,能往返。
    folder = tempfile.mkdtemp()
    pack = Path(folder, "context-pack.md")
    pack.write_text(orch.stamp_pack_fingerprint("# pack\n\nbody\n", method), encoding="utf-8")
    check("指纹可从落盘的 pack 读回", orch.read_pack_fingerprint(str(pack)) == fp_method,
          orch.read_pack_fingerprint(str(pack)))
    check("指纹是 HTML 注释,不干扰正文",
          pack.read_text(encoding="utf-8").splitlines()[0].startswith("<!--"))
    check("pack 正文完整保留", "# pack" in pack.read_text(encoding="utf-8"))

    # 三种状态必须可区分:None=首跑,""=旧版/损坏(无法证明未变,要报警),指纹串=正常。
    plain = Path(folder, "old-pack.md")
    plain.write_text("# pack without fingerprint\n", encoding="utf-8")
    check("旧版无指纹产物读出空串(不是 None)",
          orch.read_pack_fingerprint(str(plain)) == "",
          repr(orch.read_pack_fingerprint(str(plain))))
    check("文件不存在读出 None(区别于旧版产物)",
          orch.read_pack_fingerprint(str(Path(folder, "nope.md"))) is None)

    # 这是审查抓出的漏报:旧版产物必须判为"路由可能已变",不能因为空串被跳过。
    def route_changed(have) -> bool:
        return have is not None and have != fp_method

    check("首跑(None)不报警", not route_changed(None))
    check("指纹一致不报警", not route_changed(fp_method))
    check("指纹不一致要报警", route_changed(fp_results))
    check("旧版无指纹产物要报警(修复前会被静默跳过)", route_changed(""))


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


# ── 优化 4:两层检索已接成 Agent 工具 ─────────────────────────────────
def test_search_literature_formatting():
    """格式化逻辑用 mock 驱动,不碰网络。

    审查指出原测试直接调真 two_tier_search:一旦 .env 配了 RETRIEVAL_MODEL,
    这个"离线测试"会真的发网络请求。
    """
    import agents.tools as tools_mod

    def fake_search(notes, web):
        return lambda query, k=5: {"notes": notes, "web": web, "query": query}

    original = None
    if hasattr(tools_mod, "two_tier_search"):
        original = tools_mod.two_tier_search

    # 1) 两层都有命中
    hit_note = {"id": "REF-0003", "title": "Chinchilla",
                "note_path": "references/chinchilla.md", "snippet": "scaling law"}
    hit_web = {"title": "Some paper", "url": "https://arxiv.org/abs/1234",
               "snippet": "abstract text"}
    out = tools_mod._format_literature_result(
        "scaling", {"notes": [hit_note], "web": [hit_web]})
    check("Tier 1 命中渲染成表格", "REF-0003" in out and "Chinchilla" in out, out[:400])
    check("Tier 1 命中标注可 cite", "\\cite" in out, out[:400])
    check("Tier 2 命中渲染出 URL", "https://arxiv.org/abs/1234" in out, out[:600])
    check("Tier 2 明确标注不可引用",
          "LEADS ONLY" in out and "no \\cite key" in out, out[:800])

    # 2) 两层都空
    out = tools_mod._format_literature_result("nothing", {"notes": [], "web": []})
    check("Tier 1 无命中时禁止编造 cite key",
          "do NOT invent" in out, out[:400])
    check("Tier 2 无命中时说明关闭",
          "disabled" in out.lower(), out[:600])

    # 3) 返回值不是 dict → 明确错误,不抛异常
    out = tools_mod._format_literature_result("x", "not a dict")
    check("非 dict 返回值给出错误字符串", out.startswith("Error:"), out[:200])

    # 4) 字段缺失的脏数据 → 过滤掉,不 KeyError
    dirty = {"notes": [{"id": "REF-1"},                    # 缺 title/note_path/snippet
                       {"title": "no id"},                  # 无 id,应被过滤
                       "not a dict"],
             "web": [{"title": "no url"},                   # 缺 url,应被过滤
                     {"url": "https://x", "title": "ok"},
                     None]}
    out = tools_mod._format_literature_result("dirty", dirty)
    check("缺字段的 note 不会 KeyError", "REF-1" in out, out[:400])
    check("无 id 的 note 被过滤", "no id" not in out, out[:400])
    check("无 url 的 web 命中被过滤", "no url" not in out, out[:600])
    check("合法 web 命中保留", "https://x" in out, out[:600])

    # 5) two_tier_search 抛异常 → 返回错误字符串,不把异常丢给 Agent
    def boom(query, k=5):
        raise RuntimeError("network down")

    tools_mod.two_tier_search = boom
    try:
        out = tools_mod.search_literature("anything")
        check("检索抛异常时返回错误字符串", out.startswith("Error:"), out[:200])
        check("错误信息带上原因", "network down" in out, out[:200])
    finally:
        if original is not None:
            tools_mod.two_tier_search = original
        else:
            delattr(tools_mod, "two_tier_search")

    # 6) mock 正常返回 → 走完整路径
    tools_mod.two_tier_search = fake_search([hit_note], [])
    try:
        out = tools_mod.search_literature("scaling", 3)
        check("mock 驱动的完整调用返回 Tier 1 命中", "REF-0003" in out, out[:400])
    finally:
        if original is not None:
            tools_mod.two_tier_search = original
        else:
            delattr(tools_mod, "two_tier_search")

    # 7) 格式化本身抛异常也不能漏给 Agent(复审 PARTIAL 项:_format_literature_result
    #    原先在 try 之外,渲染阶段的意外异常会直接冒到 Agent 侧)
    original_fmt = tools_mod._format_literature_result

    def fmt_boom(query, result):
        raise ValueError("render exploded")

    tools_mod.two_tier_search = fake_search([hit_note], [])
    tools_mod._format_literature_result = fmt_boom
    try:
        out = tools_mod.search_literature("scaling")
        check("格式化异常也被兜住,返回错误字符串", out.startswith("Error:"), out[:200])
        check("格式化异常信息带上原因", "render exploded" in out, out[:200])
    finally:
        tools_mod._format_literature_result = original_fmt
        if original is not None:
            tools_mod.two_tier_search = original
        else:
            delattr(tools_mod, "two_tier_search")


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
        check(f"{label}_agent 持有 search_literature", "search_literature" in names, str(names))
        check(f"{label}_agent 原有工具未丢",
              {"read_file", "write_file", "list_folder"} <= names, str(names))


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items())
             if k.startswith("test_") and callable(v)]
    for test in tests:
        test()
    print(f"\n{len(PASSED)} 项检查通过 / {len(tests)} 个测试:")
    for label in PASSED:
        print(f"  ok  {label}")
    print("\nOPTIMIZATION TESTS PASSED")
