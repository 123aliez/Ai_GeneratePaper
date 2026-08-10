"""--retrieve 命令核心逻辑的离线验证（不调模型、不联网）。

覆盖：
  1. build_retrieval_prompt 注入了 idea / 章节 / data 三要素，且写明 JSON schema 约束。
  2. parse_queries 对合法 JSON 正确解析；对非法 JSON / 缺字段 / 非 list 容错降级。
  3. format_candidates 的 CAN- 前缀、空候选说明、Chapter/Query 溯源列。
  4. _dedupe_and_collect 的 URL 去重 + 来源章累积（monkeypatch search_web/verify_url）。

不调任何模型；smolagents 用最小 stub 顶掉（与 test_optimizations.py 同模式）。
运行: python tests/test_retrieve.py
"""
import json
import sys
import tempfile
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# ── stub smolagents（仅在真包缺失时启用）────────────────────────────────────
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

from agents.retrieve import (  # noqa: E402  (import 在 stub 之后)
    build_retrieval_prompt,
    parse_queries,
    validate_queries,
    format_candidates,
    _dedupe_and_collect,
)

PASSED = []


def check(label: str, cond: bool, detail: str = "") -> None:
    if not cond:
        raise AssertionError(f"{label}: {detail or 'failed'}")
    PASSED.append(label)


# ── 1. build_retrieval_prompt：三要素注入 + schema 约束 ────────────────────────
def test_prompt_injects_inputs():
    chapters = [{
        "folder": "04-method", "type": "method", "title": "Method",
        "sections": [{"title": "Overall", "bullets": ["core insight"]}],
    }]
    prompt = build_retrieval_prompt(
        chapters, idea_text="MY NOVEL METHOD XYZ", data_summary="MY DATA DUMP", out_path="out.json"
    )
    check("prompt 含 idea", "MY NOVEL METHOD XYZ" in prompt)
    check("prompt 含 data 概览", "MY DATA DUMP" in prompt)
    check("prompt 含章节 folder", "04-method" in prompt)
    check("prompt 含章节 type", "method" in prompt)
    check("prompt 写明输出路径", "out.json" in prompt)
    check("prompt 写明 JSON schema 约束", '"chapter"' in prompt and '"queries"' in prompt)


# ── 2. parse_queries：合法解析 + 容错 ──────────────────────────────────────────
def test_parse_queries_valid():
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "q.json"
        p.write_text(json.dumps([
            {"chapter": "04-method", "queries": ["q1", "q2"]},
            {"chapter": "05-results", "queries": ["r1"]},
        ]), encoding="utf-8")
        parsed = parse_queries(p)
    check("合法解析条数", len(parsed) == 2)
    check("第一章 folder 正确", parsed[0][0] == "04-method")
    check("第一章 query 数", parsed[0][1] == ["q1", "q2"])


def test_parse_queries_robust():
    with tempfile.TemporaryDirectory() as d:
        # 非法 JSON
        bad = Path(d) / "bad.json"
        bad.write_text("not json {", encoding="utf-8")
        check("非法 JSON 返回空列表", parse_queries(bad) == [])
        # 顶层非 list
        nonlist = Path(d) / "nl.json"
        nonlist.write_text(json.dumps({"x": 1}), encoding="utf-8")
        check("顶层非 list 返回空", parse_queries(nonlist) == [])
        # 混合：坏条目跳过、好条目保留
        mixed = Path(d) / "mx.json"
        mixed.write_text(json.dumps([
            "not-a-dict",
            {"chapter": "04-method", "queries": "should-be-list"},  # queries 非 list
            {"chapter": "", "queries": ["x"]},                         # 空 chapter
            {"chapter": "05-results", "queries": ["good", ""]},        # 空 query 过滤
        ]), encoding="utf-8")
        parsed = parse_queries(mixed)
    check("混合输入只留合法条目", len(parsed) == 1)
    check("合法条目 folder 正确", parsed[0][0] == "05-results")
    check("空 query 被过滤", parsed[0][1] == ["good"])
    check("不存在文件返回空", parse_queries(Path(d) / "nope.json") == [])


# ── 3. format_candidates：CAN 前缀 + 说明 + 溯源列 ─────────────────────────────
def test_format_candidates():
    out_empty = format_candidates([])
    check("空候选含说明", "候选文献清单" in out_empty and "未经人工核实" in out_empty)
    check("空候选含表头", "| ID |" in out_empty)

    hits = [{
        "id": "CAN-0001", "chapter": "04-method", "query": "channel attention",
        "title": "A Title|With Pipe", "url": "http://x/1", "snippet": "s",
    }]
    out = format_candidates(hits)
    check("CAN 前缀", "CAN-0001" in out)
    check("标题中竖线被转义", r"A Title\|With Pipe" in out)
    check("Chapter 溯源列", "| 04-method |" in out)
    check("Source Query 列", "| channel attention |" in out)
    # 列序对齐 bibliography（ID | Short Title | Authors/Year | Venue/URL | Grade | use | Chapter | Query）：
    # 用表头行验证列序（不受数据转义影响），数据行验证 url 落在 Venue/URL 位置。
    header = next(l for l in out.splitlines() if l.startswith("| ID"))
    check("表头列序对齐 bib（Venue/URL 在第4列）",
          header == "| ID | Short Title | Authors / Year | Venue / URL | Grade | One-line use | Chapter | Source Query |",
          header)
    # 数据行 url 应在 Venue/URL 列：取含 CAN-0001 的行，去掉 ID 后第一个非 url 段是 Short Title，
    # url 出现在 Authors(空) 之后。简单校验：row 中 "|  | <url> |" 模式存在。
    row = next(l for l in out.splitlines() if "CAN-0001" in l)
    check("url 在 Venue/URL 列（Authors 列空）", "|  | http://x/1 |  |  |" in row, row)


# ── 4. _dedupe_and_collect：去重 + 来源章累积（monkeypatch）────────────────────
def test_dedupe_and_collect():
    import agents.retrieve as ret  # noqa: F401
    import agents.retrieval as retr
    import agents.citation_supplement as csup

    # 两个 query 分属不同章，第一个含一条重复 url 的两条命中，第二个返回与第一条同 url（跨章）。
    fake_web = {
        "q1": [
            {"title": "A", "url": "http://dup", "snippet": "x"},
            {"title": "B", "url": "http://only1", "snippet": "y"},
        ],
        "q2": [
            {"title": "A", "url": "http://dup", "snippet": "x"},  # 与 q1 跨章重复
        ],
    }
    orig_search = retr.search_web
    orig_verify = csup.verify_url
    retr.search_web = lambda q, max_results=5: fake_web.get(q, [])
    csup.verify_url = lambda url: True  # 全部放行，专注测去重逻辑
    try:
        hits, stats = _dedupe_and_collect([
            ("04-method", ["q1"]),
            ("05-results", ["q2"]),
        ])
    finally:
        retr.search_web = orig_search
        csup.verify_url = orig_verify

    check("去重后候选数 = 唯一 url 数", len(hits) == 2, str(len(hits)))
    check("CAN 编号连续", [h["id"] for h in hits] == ["CAN-0001", "CAN-0002"])
    dup_hit = next(h for h in hits if h["url"] == "http://dup")
    check("跨章重复 url 的来源章累积",
          dup_hit["chapter"] == "04-method 05-results", dup_hit["chapter"])
    check("stats queries 计数", stats["queries"] == 2, str(stats))
    check("stats raw_hits 计数", stats["raw_hits"] == 3, str(stats))
    check("stats duplicates 计数", stats["duplicates"] == 1, str(stats))


def test_dedupe_filtered_by_verify():
    """verify_url 不过的命中被过滤，不进候选。"""
    import agents.retrieve as ret  # noqa: F401
    import agents.retrieval as retr
    import agents.citation_supplement as csup

    fake = [{"title": "dead", "url": "http://dead", "snippet": "x"},
            {"title": "alive", "url": "http://alive", "snippet": "y"}]
    orig_search = retr.search_web
    orig_verify = csup.verify_url
    retr.search_web = lambda q, max_results=5: fake
    csup.verify_url = lambda url: url == "http://alive"
    try:
        hits, stats = _dedupe_and_collect([("04-method", ["q1"])])
    finally:
        retr.search_web = orig_search
        csup.verify_url = orig_verify

    check("verify_url 过滤后只剩存活项", len(hits) == 1, str(len(hits)))
    check("存活项 url 正确", hits[0]["url"] == "http://alive")
    check("stats filtered 计数", stats["filtered"] == 1, str(stats))


# ── 5. validate_queries：契约校验（漏章/超量/重复/错序）────────────────────────
def test_validate_queries():
    chapters = [{"folder": "04-method"}, {"folder": "05-results"}]

    # 合法：逐章覆盖、顺序一致、每章 2-4 条
    validate_queries([("04-method", ["a", "b"]), ("05-results", ["c", "d"])], chapters)
    check("合法契约通过", True)

    def expect_fail(label, parsed, chapters_arg):
        try:
            validate_queries(parsed, chapters_arg)
            check(label, False, "应抛 ValueError")
        except ValueError:
            check(label, True)

    expect_fail("漏章拒绝", [("04-method", ["a", "b"])], chapters)
    expect_fail("未知章拒绝", [("99-unknown", ["a", "b"]), ("05-results", ["c", "d"])], chapters)
    expect_fail("顺序错拒绝", [("05-results", ["a", "b"]), ("04-method", ["c", "d"])], chapters)
    expect_fail("每章 <2 条拒绝", [("04-method", ["only"]), ("05-results", ["c", "d"])], chapters)
    expect_fail("每章 >4 条拒绝", [("04-method", ["a", "b", "c", "d", "e"]), ("05-results", ["f", "g"])], chapters)
    expect_fail("重复 query 拒绝", [("04-method", ["dup", "dup"]), ("05-results", ["c", "d"])], chapters)


tests = [
    test_prompt_injects_inputs,
    test_parse_queries_valid, test_parse_queries_robust,
    test_format_candidates,
    test_dedupe_and_collect, test_dedupe_filtered_by_verify,
    test_validate_queries,
]

if __name__ == "__main__":
    print("== test_retrieve ==")
    total = len(tests)
    for t in tests:
        t()
    print(f"\n{len(PASSED)} 项检查通过 / {total} 个测试:")
    for label in PASSED:
        print(f"  ✓ {label}")
    print("\nRETRIEVE TESTS PASSED")
