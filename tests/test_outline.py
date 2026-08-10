"""outline.md 解析与章节工作区生成的离线验证(不调 API、不发网络)。

运行: python tests/test_outline.py
"""
import hashlib
import sys
import tempfile
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# smolagents 缺失时用最小 stub 顶掉(被测的是纯 Python 逻辑)
try:
    import smolagents  # noqa: F401
except ModuleNotFoundError:
    stub = types.ModuleType("smolagents")
    stub.tool = lambda fn: fn

    class _Agent:
        def __init__(self, *a, **kw):
            self.tools = kw.get("tools", [])

    stub.CodeAgent = _Agent
    stub.Model = object
    stub.OpenAIModel = object
    sys.modules["smolagents"] = stub

import agents.outline as ol
import agents.orchestrator as orch

PASSED = []


def check(label: str, cond: bool, detail: str = "") -> None:
    if not cond:
        raise AssertionError(f"{label}: {detail or 'failed'}")
    PASSED.append(label)


SAMPLE = """\
# Paper outline — Spec Module

> 说明文字,不该被当成章节。

```
## 99. 这是代码块里的示例
type: results
```

## 1. Abstract

type: abstract

### 1.1 摘要正文 (~150 words)
- 一句问题、一句方法、一句结果

## 4. Method

type: method

### 4.1 总体框架 (~250 words)
- 输入输出与符号约定
- 不要写实验结果
### 4.2 Spec 模块 (~350 words)
- 从 idea.md 展开机制
### 4.3 复杂度 (~200 words)
- 参数量与开销

## 5. Results

type: results

### 5.1 主结果 (~300 words)
- 只用 data/ 里的真实数字
### 5.2 消融 (~250 words)
- type: ablation
- 逐项拆组件
"""


def write_outline(text: str) -> str:
    path = Path(tempfile.mkdtemp()) / "outline.md"
    path.write_text(text, encoding="utf-8")
    return str(path)


# ── 解析 ─────────────────────────────────────────────────────────────
def test_parse_outline():
    chapters = ol.parse_outline(write_outline(SAMPLE))
    check("解析出 3 章", len(chapters) == 3, str([c["title"] for c in chapters]))
    check("代码块里的 ## 不被当成章",
          all(c["number"] != 99 for c in chapters), str([c["number"] for c in chapters]))

    abstract, method, results = chapters
    check("章号取自标题", (abstract["number"], method["number"], results["number"]) == (1, 4, 5))
    check("章标题正确", method["title"] == "Method", method["title"])
    check("章级 type 解析", method["type"] == "method", method["type"])
    check("type 决定 family", method["family"] == "idea", method["family"])
    check("type 决定 gate", results["gate"] == "blocking", results["gate"])
    check("abstract 章 gate 是 advisory", abstract["gate"] == "advisory", abstract["gate"])

    check("Method 有 3 个小节", len(method["sections"]) == 3, str(len(method["sections"])))
    check("小节按章内序号重编号",
          [s["number"] for s in method["sections"]] == [1, 2, 3],
          str([s["number"] for s in method["sections"]]))
    check("小节标题去掉了 outline 里的编号",
          method["sections"][1]["title"] == "Spec 模块", method["sections"][1]["title"])
    check("小节字数解析", method["sections"][1]["target_words"] == 350,
          str(method["sections"][1]["target_words"]))
    check("小节要点收集", method["sections"][0]["bullets"] == ["输入输出与符号约定", "不要写实验结果"],
          str(method["sections"][0]["bullets"]))
    check("小节级 type 覆盖被记录",
          results["sections"][1]["type"] == "ablation", results["sections"][1]["type"])
    check("小节级 type 行不被当成要点",
          "type: ablation" not in " ".join(results["sections"][1]["bullets"]),
          str(results["sections"][1]["bullets"]))

    check("文件夹名带零填充章号与 type",
          method["folder"] == "04-method", method["folder"])
    check("文件夹名可被 chapter_type 反推",
          "method" in method["folder"])


def test_parse_edge_cases():
    check("outline 不存在返回空表", ol.parse_outline("/nonexistent/outline.md") == [])

    # 无章号 → 按出现顺序自动编号
    chapters = ol.parse_outline(write_outline(
        "## Introduction\ntype: intro\n### 背景 (~200 words)\n- x\n"
        "## Method\ntype: method\n### 设计 (~200 words)\n- y\n"))
    check("无章号时自动编号", [c["number"] for c in chapters] == [1, 2],
          str([c["number"] for c in chapters]))

    # 缺字数 → 按 type 查表给默认值(method=1500),不是清一色 250
    chapters = ol.parse_outline(write_outline(
        "## 1. Method\ntype: method\n### 设计\n- x\n"))
    check("method 章小节缺字数按 type 给默认 1500",
          chapters[0]["sections"][0]["target_words"] == ol.DEFAULT_WORDS_BY_TYPE["method"],
          str(chapters[0]["sections"][0]["target_words"]))
    check("缺字数小节 words_explicit 仍是 False(默认值不算作者的决定)",
          chapters[0]["sections"][0]["words_explicit"] is False)

    # 不同 type 给不同默认值
    results = ol.parse_outline(write_outline("## 1. Results\ntype: results\n### 主结果\n- x\n"))
    check("results 章缺字数给 1800",
          results[0]["sections"][0]["target_words"] == ol.DEFAULT_WORDS_BY_TYPE["results"])

    # 小节级 type 优先于章 type:results 章里一个 ablation 小节按 1200
    mixed = ol.parse_outline(write_outline(
        "## 1. Results\ntype: results\n### 主结果\n- x\n\n### 消融\n- type: ablation\n- y\n"))
    check("小节级 type 覆盖章 type 的默认字数",
          mixed[0]["sections"][0]["target_words"] == ol.DEFAULT_WORDS_BY_TYPE["results"],
          str(mixed[0]["sections"][0]["target_words"]))
    check("ablation 小节按 ablation 的默认字数 1200",
          mixed[0]["sections"][1]["target_words"] == ol.DEFAULT_WORDS_BY_TYPE["ablation"],
          str(mixed[0]["sections"][1]["target_words"]))

    # 显式标注优先于查表默认
    explicit = ol.parse_outline(write_outline("## 1. Method\ntype: method\n### 设计 (~400 words)\n- x\n"))
    check("显式标字数优先于 type 默认值",
          explicit[0]["sections"][0]["target_words"] == 400,
          str(explicit[0]["sections"][0]["target_words"]))
    check("显式标了 words_explicit=True",
          explicit[0]["sections"][0]["words_explicit"] is True)

    # 无法识别的 type 没有默认字数 → 退回兜底 250
    unknown = ol.parse_outline(write_outline("## 1. Foo\ntype: 火星文\n### A\n- x\n"))
    check("无法识别 type 的小节退回兜底 250",
          unknown[0]["sections"][0]["target_words"] == ol.DEFAULT_SECTION_WORDS,
          str(unknown[0]["sections"][0]["target_words"]))

    # 章级没写 type → 从标题推
    chapters = ol.parse_outline(write_outline("## 5. Results\n### 主结果 (~300 words)\n- x\n"))
    check("章级缺 type 时从标题推断", chapters[0]["type"] == "results", chapters[0]["type"])

    # 无法识别的 type → 退回 mixed/advisory 并记录原文
    chapters = ol.parse_outline(write_outline("## 1. Foo\ntype: 火星文\n### A (~100 words)\n- x\n"))
    check("无法识别的 type 退回 mixed", chapters[0]["family"] == "mixed", chapters[0]["family"])
    check("无法识别的 type 被记录", chapters[0]["unrecognized"] == "火星文",
          chapters[0]["unrecognized"])

    # 模板占位符不算声明
    chapters = ol.parse_outline(write_outline(
        "## 1. Method\ntype: <method|results>\n### A (~100 words)\n- x\n"))
    check("`type: <...>` 占位符不被当成声明",
          chapters[0]["type"] == "method",  # 退回标题推断
          chapters[0]["type"])

    # 没有小节但声明了 type 的章:合法(还没写小节),保留
    chapters = ol.parse_outline(write_outline("## 1. Abstract\ntype: abstract\n"))
    check("无小节但有 type 的章仍被解析", len(chapters) == 1)
    check("无小节的章 sections 为空", chapters[0]["sections"] == [])

    # 非章节标题必须被丢掉。outline.md 顶部一般有「写法」「说明」这类 `##` 小标题,
    # 把它们当成章会生成一个 01-unknown 空文件夹。
    chapters = ol.parse_outline(write_outline(
        "# Paper outline\n\n"
        "## 写法\n"
        "随便写点说明,没有 type 也没有小节。\n\n"
        "## 注意事项\n"
        "更多说明文字。\n\n"
        "## 1. Method\ntype: method\n### 设计 (~200 words)\n- x\n"))
    check("没有 type、没有小节、标题也推不出类型的 ## 被丢掉",
          len(chapters) == 1, str([c["title"] for c in chapters]))
    check("真章节被保留", chapters[0]["title"] == "Method", chapters[0]["title"])

    # 但标题本身能推出类型的要保留(即使没写 type、没写小节)
    chapters = ol.parse_outline(write_outline("## Results\n"))
    check("标题能推出类型的 ## 被保留", len(chapters) == 1, str(chapters))


# ── brief 渲染 ───────────────────────────────────────────────────────
def test_render_brief_is_parseable():
    """生成的 brief 必须能被流水线自己的解析器读回来——这是两边格式的契约。"""
    chapters = ol.parse_outline(write_outline(SAMPLE))
    method = next(c for c in chapters if c["title"] == "Method")

    folder = Path(tempfile.mkdtemp())
    (folder / "brief.md").write_text(ol.render_brief(method), encoding="utf-8")

    sections = orch.parse_brief_sections(str(folder))
    check("orchestrator 能解析出 3 个小节", len(sections) == 3, str(sections))
    check("小节标题往返一致",
          [s["title"] for s in sections] == ["总体框架", "Spec 模块", "复杂度"],
          str([s["title"] for s in sections]))
    check("小节字数往返一致",
          [s["target_words"] for s in sections] == [250, 350, 200],
          str([s["target_words"] for s in sections]))

    from agents.chapter_type import resolve_run_route
    route = resolve_run_route(str(folder), sections)
    check("route 从生成的 brief 里读出 type", route["type"] == "method", str(route))
    check("route 的 source 是 brief(不是靠文件夹名兜底)",
          route["source"] == "brief", route["source"])
    check("route family 正确", route["family"] == "idea", route["family"])

    # 小节级覆盖也要能往返
    results = next(c for c in chapters if c["title"] == "Results")
    folder2 = Path(tempfile.mkdtemp())
    (folder2 / "brief.md").write_text(ol.render_brief(results), encoding="utf-8")
    sections2 = orch.parse_brief_sections(str(folder2))
    route2 = resolve_run_route(str(folder2), sections2)
    check("小节级 type 覆盖往返有效",
          route2["section_types"].get(2) == "ablation", str(route2["section_types"]))


def test_brief_fingerprint():
    chapters = ol.parse_outline(write_outline(SAMPLE))
    method = next(c for c in chapters if c["title"] == "Method")

    folder = Path(tempfile.mkdtemp())
    brief = folder / "brief.md"
    brief.write_text(ol.render_brief(method), encoding="utf-8")

    fp = ol.chapter_fingerprint(method)
    check("指纹可从 brief 首行读回", ol.read_brief_fingerprint(str(brief)) == fp,
          ol.read_brief_fingerprint(str(brief)))
    check("指纹是 HTML 注释",
          brief.read_text(encoding="utf-8").startswith("<!-- outline-fingerprint:"))
    # 必须是跨进程稳定的哈希。用内置 hash() 的话,--init 与实际运行是两次启动、
    # PYTHONHASHSEED 不同,指纹就永远对不上,每次运行都误报 brief 过期。
    check("要点摘要用稳定的 sha256,不是进程随机化的 hash()",
          hashlib.sha256("|".join(method["sections"][0]["bullets"]).encode("utf-8")
                         ).hexdigest()[:12] in fp, fp)

    # 改要点要换指纹(否则改了指令却不重新 --init,Agent 拿到的还是旧的)
    changed = dict(method)
    changed["sections"] = [dict(s) for s in method["sections"]]
    changed["sections"][0] = dict(changed["sections"][0],
                                  bullets=["完全不同的要点"])
    check("改要点会改变指纹", ol.chapter_fingerprint(changed) != fp)

    # 改字数、改类型同样换指纹
    reworded = dict(method)
    reworded["sections"] = [dict(s) for s in method["sections"]]
    reworded["sections"][0] = dict(reworded["sections"][0], target_words=999)
    check("改字数会改变指纹", ol.chapter_fingerprint(reworded) != fp)
    check("改章级 type 会改变指纹",
          ol.chapter_fingerprint(dict(method, type="results")) != fp)

    # 手写的 brief 没有指纹 → 返回 ""(不报错,手写路径照常可用)
    hand = folder / "hand.md"
    hand.write_text("# 手写 brief\n\ntype: method\n", encoding="utf-8")
    check("手写 brief 无指纹返回空串", ol.read_brief_fingerprint(str(hand)) == "")
    check("文件不存在返回空串",
          ol.read_brief_fingerprint(str(folder / "nope.md")) == "")


# ── 工作区生成 ───────────────────────────────────────────────────────
def test_init_creates_workspaces():
    outline = write_outline(SAMPLE)
    ws = tempfile.mkdtemp()
    result = ol.init_chapter_workspaces(outline, ws)

    check("三章都被创建", len(result["created"]) == 3, str(result["created"]))
    check("文件夹按章号命名",
          sorted(result["created"]) == ["01-abstract", "04-method", "05-results"],
          str(sorted(result["created"])))
    for name in result["created"]:
        check(f"{name}/brief.md 已生成", (Path(ws, name, "brief.md")).exists())

    # input.md 不再由 --init 生成:它改由 Stage 0 时 Manager 从参考文献组织。
    check("input.md 不再由 --init 生成(改由 Stage 0)",
          not (Path(ws, "04-method", "input.md")).exists())


def test_init_never_clobbers_user_edits():
    outline = write_outline(SAMPLE)
    ws = tempfile.mkdtemp()
    ol.init_chapter_workspaces(outline, ws)

    # 用户手改 brief 与 input
    brief = Path(ws, "04-method", "brief.md")
    user_input = Path(ws, "04-method", "input.md")
    brief.write_text("# 我手改过的 brief\n\ntype: method\n", encoding="utf-8")
    user_input.write_text("我的实验素材\n", encoding="utf-8")

    again = ol.init_chapter_workspaces(outline, ws)
    check("已存在的 brief 被跳过而非覆盖",
          "04-method" in again["skipped"] or "04-method" in again["stale"],
          str(again))
    check("手改的 brief 内容仍在", "我手改过的" in brief.read_text(encoding="utf-8"))
    check("手填的 input 内容仍在(--init 不碰 input.md)",
          "我的实验素材" in user_input.read_text(encoding="utf-8"))

    # --force 才覆盖 brief,input 不由 --init 管,自然不动
    forced = ol.init_chapter_workspaces(outline, ws, force=True)
    check("--force 会刷新 brief", "04-method" in forced["updated"], str(forced))
    check("--force 后 brief 是生成版本",
          "outline-fingerprint" in brief.read_text(encoding="utf-8"))
    check("--force 也不碰 input.md",
          "我的实验素材" in user_input.read_text(encoding="utf-8"))


def test_init_detects_stale_brief():
    """outline 改了但没 --force,该章必须被标成 stale 而不是静默跳过。"""
    outline_path = write_outline(SAMPLE)
    ws = tempfile.mkdtemp()
    ol.init_chapter_workspaces(outline_path, ws)

    # 改 outline:给 Method 的第一节换要点
    text = Path(outline_path).read_text(encoding="utf-8")
    Path(outline_path).write_text(
        text.replace("- 输入输出与符号约定", "- 换成完全不同的要点"), encoding="utf-8")

    again = ol.init_chapter_workspaces(outline_path, ws)
    check("outline 改动后该章被标为 stale",
          "04-method" in again["stale"], str(again))
    check("未改动的章仍是 skipped",
          "01-abstract" in again["skipped"], str(again))


# ── 邻章视野 ─────────────────────────────────────────────────────────
def test_outline_excerpt_is_bounded():
    outline = write_outline(SAMPLE)

    excerpt = ol.build_outline_excerpt("04-method", outline)
    check("摘要标明本章", ">> 4. Method" in excerpt, excerpt)
    check("摘要含前一章", "<- 1. Abstract" in excerpt, excerpt)
    check("摘要含后一章", "-> 5. Results" in excerpt, excerpt)
    check("摘要列出各章小节标题", "Spec 模块" in excerpt and "主结果" in excerpt, excerpt)
    # 有界:只三章,且不含要点正文
    check("摘要不含要点正文", "从 idea.md 展开机制" not in excerpt, excerpt)
    check("摘要明确禁止代写邻章",
          "do not draft content for the neighbouring chapters" in excerpt, excerpt)

    first = ol.build_outline_excerpt("01-abstract", outline)
    check("首章没有前一章", "<-" not in first, first)
    check("首章有后一章", "-> 4. Method" in first, first)

    last = ol.build_outline_excerpt("05-results", outline)
    check("末章没有后一章", "->" not in last, last)
    check("末章有前一章", "<- 4. Method" in last, last)

    # 不在 outline 里的文件夹不是合法章节,resolve 抛错(这里只验证它确实报错)。
    try:
        ol.build_outline_excerpt("99-handmade", outline)
    except ol.OutlineRouteError:
        check("outline 里没有的文件夹报错", True)
    else:
        raise AssertionError("不在 outline 的文件夹没报错")
    # outline 不存在同样报错。
    try:
        ol.build_outline_excerpt("04-method", "/nonexistent")
    except ol.OutlineRouteError:
        check("outline 不存在时报错", True)
    else:
        raise AssertionError("outline 不存在时没报错")


# ── 段数自适应(与 outline 配合的关键行为) ────────────────────────────
def test_part_count_adapts_to_section_count():
    """1/2 个小节的章不再被硬凑成三段。

    Abstract 常常只有 1 个小节;旧行为会补出 "Chapter part 2/3" 两个没有来源的
    空段,还各给 700 词目标,于是 150 词的摘要被要求写成两千词。
    """
    def sections(n):
        return [{"number": i, "title": f"S{i}", "target_words": 150}
                for i in range(1, n + 1)]

    for count, want_parts in ((1, 1), (2, 2), (3, 3), (6, 3), (7, 3)):
        parts = orch.build_stage1_parts(sections(count))
        check(f"{count} 个小节 → {want_parts} 段", len(parts) == want_parts,
              f"got {len(parts)}")
        # 每一段都必须覆盖真实小节,不能是凭空的 Chapter part N
        for part in parts:
            check(f"{count} 小节:第 {part['index']} 段有真实小节",
                  bool(part["numbers"]), str(part))

    # 单小节章的目标字数就是那一节的字数,不是 700
    single = orch.build_stage1_parts(sections(1))
    check("单小节章的目标字数取自该小节", single[0]["target_words"] == 150,
          str(single[0]["target_words"]))

    # 没有小节(brief 格式写错)→ 单段兜底
    none = orch.build_stage1_parts([])
    check("无小节时退回单段", len(none) == 1, str(len(none)))
    check("无小节时使用兜底标题", none[0]["titles"] == ["Chapter part 1"], str(none[0]))


def test_concatenate_respects_part_count():
    """段数变少时,上一次跑三段留下的 part-3 不能被拼进来。"""
    folder = Path(tempfile.mkdtemp())
    for index, text in ((1, "第一段"), (2, "第二段"), (3, "上一次的旧第三段")):
        (folder / f"draft-v1.part-{index}.md").write_text(text, encoding="utf-8")

    orch.concatenate_stage1_parts(str(folder), part_count=2)
    draft = (folder / "draft-v1.md").read_text(encoding="utf-8")
    check("只拼本次的两段", "第一段" in draft and "第二段" in draft, draft)
    check("旧的第三段没被拼进来", "旧第三段" not in draft, draft)

    orch.concatenate_stage1_parts(str(folder), part_count=3)
    draft3 = (folder / "draft-v1.md").read_text(encoding="utf-8")
    check("三段时全部拼入", "旧第三段" in draft3, draft3)


# ── 写作路由 ─────────────────────────────────────────────────────────
def test_write_mode_resolution():
    """章节文件夹必须出现在 outline.md 里,否则不是合法章节。"""
    outline = write_outline(SAMPLE)

    method = ol.resolve_write_mode("04-method", outline)
    check("outline 里的章解析为 FULL", method["mode"] == ol.FULL, str(method))
    check("知道自己是第几章", (method["position"], method["total"]) == (2, 3),
          str((method["position"], method["total"])))
    check("拿到前一章", method["prev"]["title"] == "Abstract", str(method["prev"]))
    check("拿到后一章", method["next"]["title"] == "Results", str(method["next"]))

    first = ol.resolve_write_mode("01-abstract", outline)
    check("首章没有前一章", first["prev"] is None, str(first["prev"]))
    last = ol.resolve_write_mode("05-results", outline)
    check("末章没有后一章", last["next"] is None, str(last["next"]))

    # 不在 outline 里的文件夹不再是合法章节,直接报错(引导去 --init)。
    try:
        ol.resolve_write_mode("my-chapter", outline)
    except ol.OutlineRouteError as exc:
        check("不在 outline 的文件夹抛错", True)
        check("错误信息引导 --init", "--init" in str(exc), str(exc))
    else:
        raise AssertionError("不在 outline 的文件夹被当成了合法章节")

    # outline 不存在同样报错。
    try:
        ol.resolve_write_mode("04-method", "/nonexistent/outline.md")
    except ol.OutlineRouteError as exc:
        check("outline 缺失时抛错", True)
        check("缺失错误引导 --init", "--init" in str(exc), str(exc))
    else:
        raise AssertionError("outline 缺失时没有报错")

    # outline 存在但解析不出章节 = 结构文件写坏了,报错而不是静默继续。
    broken = write_outline("# 只有说明,没有任何章节标题\n\n随便写点什么。\n")
    try:
        ol.resolve_write_mode("04-method", broken)
    except ol.OutlineRouteError as exc:
        check("outline 存在但解析为空时报错", True)
        check("空 outline 错误引导 --init", "--init" in str(exc), str(exc))
    else:
        raise AssertionError("坏掉的 outline 没有报错")

    # 两章映射到同一文件夹:邻章位置无法确定,next() 只会取到第一个匹配。
    duplicate = write_outline(
        "## 1. Method\n\ntype: method\n\n### A (~100 words)\n- x\n\n"
        "## 1. Method\n\ntype: method\n\n### B (~100 words)\n- y\n")
    try:
        ol.parse_outline(duplicate)
    except ol.OutlineRouteError:
        check("重复文件夹映射被拒绝", True)
    else:
        raise AssertionError("重复文件夹映射未被检测")


def test_mode_clause_is_full_paper():
    """写作契约只此一种:整篇第 N 章,符号沿用前章。"""
    outline = write_outline(SAMPLE)
    full = ol.build_mode_clause("04-method", outline, cross_chapter_path="ws/xchap.md")

    check("声明是整篇的第几章", "chapter 2 of 3" in full, full[:200])
    check("指向跨章状态文件", "ws/xchap.md" in full, full[:400])
    check("禁止重复定义前章已定的符号",
          "Do NOT re-define" in full, full[:400])
    check("允许跨章引用", "Cross-references to other chapters ARE allowed" in full,
          full[:600])
    check("中间章要接上文", "preceding chapter" in full and "Abstract" in full, full)
    check("中间章要引下文", "following chapter" in full and "Results" in full, full)

    # 首章/末章的开合指令
    first = ol.build_mode_clause("01-abstract", outline)
    check("首章不要求接上文", "FIRST chapter" in first, first)
    last = ol.build_mode_clause("05-results", outline)
    check("末章不要求引下文", "LAST chapter" in last, last)

    # 审稿侧同样:查重复定义与跨章重复
    r_full = ol.build_mode_review_clause("04-method", outline, cross_chapter_path="ws/x.md")
    check("审稿查重复定义与跨章重复",
          "re-defines" in r_full and "duplicates material" in r_full, r_full)


def test_init_creates_cross_chapter_state():
    """跨章状态文件由 --init 建,是跨章术语与结论的载体。"""
    outline = write_outline(SAMPLE)
    ws = Path(tempfile.mkdtemp())
    result = ol.init_chapter_workspaces(outline, str(ws))

    xchap = ws / ol.CROSS_CHAPTER_STATE
    check("--init 生成跨章状态文件", xchap.exists(), str(list(ws.iterdir())))
    check("首次生成标记为 created", result["cross_chapter"] == "created",
          result["cross_chapter"])
    text = xchap.read_text(encoding="utf-8")
    check("跨章状态列出章节顺序",
          "01-abstract" in text and "04-method" in text and "05-results" in text, text)
    for heading in ("## Terminology Decisions", "## Per-Chapter Key Claims",
                    "## Unresolved Cross-Chapter Issues"):
        check(f"跨章状态含 {heading}", heading in text, text)

    # 再跑一次不覆盖
    again = ol.init_chapter_workspaces(outline, str(ws))
    check("已存在的跨章状态被跳过", again["cross_chapter"] == "skipped",
          again["cross_chapter"])


def test_force_init_keeps_accumulated_cross_chapter_content():
    """--force 只刷新顶部章节清单;三个小节里积累的术语约定必须留下。

    冲掉它等于让后续章节失去对齐依据——前面几章跑出来的术语决定就没了。
    """
    outline = write_outline(SAMPLE)
    ws = Path(tempfile.mkdtemp())
    ol.init_chapter_workspaces(outline, str(ws))
    xchap = ws / ol.CROSS_CHAPTER_STATE

    text = xchap.read_text(encoding="utf-8")
    xchap.write_text(text.replace(
        "## Terminology Decisions\n",
        "## Terminology Decisions\n\n- fine-tuning(不写 finetuning)\n"), encoding="utf-8")

    result = ol.init_chapter_workspaces(outline, str(ws), force=True)
    after = xchap.read_text(encoding="utf-8")
    check("--force 标记为 updated", result["cross_chapter"] == "updated",
          result["cross_chapter"])
    check("--force 保留已积累的术语约定", "fine-tuning" in after, after)
    check("--force 后顶部章节清单仍在", "01-abstract" in after, after)
    check("--force 后三个小节标题都还在",
          all(h in after for h in ol.XCHAP_HEADINGS), after)

    # 标题被手改 → 拒绝写入。否则 --force 会"刷新成功"而已积累的术语约定实际
    # 已经丢了,后面几章从此各写一套,没人报错。
    damaged = after.replace("## Terminology Decisions",
                            "## Terminology (被使用者改名了)")
    xchap.write_text(damaged, encoding="utf-8")
    try:
        ol.init_chapter_workspaces(outline, str(ws), force=True)
    except ol.CrossChapterStateError:
        check("标题被改名时 --force 拒绝覆盖", True)
    else:
        raise AssertionError("结构损坏的跨章状态被伪装成刷新成功")
    check("拒绝刷新后原文件一个字节都没改",
          xchap.read_text(encoding="utf-8") == damaged)


def test_outline_banner_reports_cross_chapter():
    outline = write_outline(SAMPLE)
    ws = tempfile.mkdtemp()
    lines = ol.outline_banner(ol.init_chapter_workspaces(outline, ws))
    check("--init 汇报里提到跨章状态文件",
          any(ol.CROSS_CHAPTER_STATE in line for line in lines), str(lines))


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items())
             if k.startswith("test_") and callable(v)]
    for test in tests:
        test()
    print(f"\n{len(PASSED)} 项检查通过 / {len(tests)} 个测试:")
    for label in PASSED:
        print(f"  ok  {label}")
    print("\nOUTLINE TESTS PASSED")
