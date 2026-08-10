"""outline --expand 的离线测试(不调 API)。

覆盖的是这条链的两端:提示词是否真的把「不许改结构」写进去了,以及 Manager 万一
改了结构,validate_expansion 能不能全部拦住。中间那段(真模型的输出质量)只有真
跑才知道,不在这里假装验证。
"""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agents.outline import chapters_without_sections, parse_outline
from agents.outline_expand import (
    EXPANDED_MARKER, build_expand_prompt, missing_word_counts,
    outline_skeleton_digest, render_outline, skeleton_report,
    validate_expansion,
)

CHECKS = 0
FAILURES = []


def check(label: str, condition, detail: str = ""):
    global CHECKS
    CHECKS += 1
    if condition:
        print(f"  ok   | {label}")
    else:
        print(f"  FAIL | {label}" + (f" — {detail}" if detail else ""))
        FAILURES.append(label)


def write_outline(text: str) -> Path:
    path = Path(tempfile.mkdtemp()) / "outline.md"
    path.write_text(text, encoding="utf-8")
    return path


SKELETON = """# Paper outline — Spectral Recalibration

## 1. Abstract

type: abstract

## 2. Introduction

type: intro

## 3. 谱域重标定

type: method

## 4. Results

type: results
"""


def test_skeleton_parses_without_sections():
    """只有章标题 + type 的骨架必须能解析——这正是 --expand 的输入形态。"""
    print("\n[骨架解析]")
    chapters = parse_outline(write_outline(SKELETON))
    check("4 章全部解析出来", len(chapters) == 4, f"got {len(chapters)}")
    check("没有小节也算合法章", all(c["sections"] == [] for c in chapters))
    check("type 逐章正确",
          [c["type"] for c in chapters] == ["abstract", "intro", "method", "results"],
          str([c["type"] for c in chapters]))
    check("中文章标题不影响 type",
          chapters[2]["type"] == "method" and chapters[2]["title"] == "谱域重标定")
    check("文件夹名带上 type 兜底", chapters[2]["folder"].endswith("-method"),
          chapters[2]["folder"])
    report = skeleton_report(chapters)
    check("骨架清单每章一行", len(report) == 4)
    check("骨架清单标出待展开", all("待展开" in line for line in report))


def test_prompt_states_the_hard_constraints():
    """提示词里的约束是这个功能唯一的护栏,逐条断言它们在场。"""
    print("\n[展开提示词]")
    chapters = parse_outline(write_outline(SKELETON))
    prompt = build_expand_prompt(chapters, "/tmp/out.md", "/tmp/out.zh.md",
                                 "/tmp/idea.md", 4)

    check("要求先读 idea.md", "/tmp/idea.md" in prompt)
    check("写明英文产物路径", "/tmp/out.md" in prompt)
    check("写明中文产物路径", "/tmp/out.zh.md" in prompt)
    check("要求写两份", "TWO files" in prompt or "two files" in prompt)
    check("要求英文版给 Agent", "ENGLISH version" in prompt or "out_en_path" in prompt)
    check("禁止改章节结构",
          "Do NOT add, remove, merge, split, reorder or rename a chapter" in prompt)
    check("禁止改 type", "Do NOT change any chapter's `type:` line" in prompt)
    check("禁止写字数", "Do NOT write word counts" in prompt)
    check("要求写边界约束", "WHICH BOUNDARY not to cross" in prompt)
    check("要求缺失处打标记", "[DESIGN DETAIL NEEDED]" in prompt
          and "[MISSING DATA]" in prompt)
    check("禁止写衔接与术语要点(框架自己管)",
          "Do not write bullets about chapter transitions" in prompt)
    check("限定 2~4 个小节", "2 to 4 subsections" in prompt)
    check("解释了三段起草的原因", "drafted in three separate passes" in prompt)
    for chapter in chapters:
        check(f"骨架里带上了第 {chapter['number']} 章",
              f"{chapter['number']}. {chapter['title']}" in prompt)
    check("带上了作者的 type 值", "(type: method" in prompt)


def test_prompt_preserves_authors_existing_sections():
    """作者已经写了小节的章,提示词必须要求原样保留标题与顺序。"""
    print("\n[已有小节的章]")
    chapters = parse_outline(write_outline(
        SKELETON + "\n### 4.1 主结果 (~300 words)\n- 只用结果库的真实数字\n"))
    prompt = build_expand_prompt(chapters, "/tmp/out.md", "/tmp/out.zh.md",
                                 "/tmp/idea.md", 4)
    check("提示里出现作者写的小节标题", "主结果" in prompt)
    check("提示里出现作者写的要点", "只用结果库的真实数字" in prompt)
    check("要求逐字保留作者的小节",
          "KEEP their titles and order verbatim" in prompt)
    check("标注了作者已写几个小节", "作者已写了 1 个小节" in prompt)


def _expanded(replace: dict | None = None) -> list[dict]:
    """一份合法的展开结果,replace 用来注入越权改动。

    abstract 是短章：用新风格（要点挂 ## 下，合成小节），不用 ### 1.1。这样与短章
    "单段起草"设计一致，也通过 validate_expansion 的"裸短章不得新增 ###"校验。
    """
    text = """# expanded

## 1. Abstract

type: abstract

- 一句问题一句方法一个 headline 数

## 2. Introduction

type: intro

### 2.1 背景与问题
- 领域现状与现有方法的不足

## 3. 谱域重标定

type: method

### 3.1 总体框架
- 输入输出与数据流
### 3.2 频域重标定算子
- 从 idea.md 第 4 节展开机制
- 只讲机制,测得的数字留给结果章

## 4. Results

type: results

### 4.1 主结果
- 只用结果库里的真实数字
"""
    for old, new in (replace or {}).items():
        text = text.replace(old, new)
    return parse_outline(write_outline(text))


def test_valid_expansion_passes():
    print("\n[合法展开]")
    original = parse_outline(write_outline(SKELETON))
    problems = validate_expansion(original, _expanded())
    check("合法展开无问题", problems == [], str(problems))
    expanded = _expanded()
    check("小节数累计正确",
          sum(len(c["sections"]) for c in expanded) == 5,
          str([len(c["sections"]) for c in expanded]))
    check("骨架摘要保持一致",
          outline_skeleton_digest(original) == outline_skeleton_digest(expanded))
    # abstract 现在是合成小节（words_explicit=False），计入"待标字数"；
    # 其余 4 个小节也是 words_explicit=False。共 5 个待标。
    check("没写字数的起草单元全部计入待补",
          missing_word_counts(expanded) == 5, str(missing_word_counts(expanded)))


def test_structural_tampering_is_caught():
    """越权改结构的五种形态,逐个必须被拦。"""
    print("\n[越权改结构]")
    original = parse_outline(write_outline(SKELETON))

    renamed = validate_expansion(original, _expanded(
        {"## 3. 谱域重标定": "## 3. Spectral Recalibration"}))
    check("改章标题被拦", any("标题被改" in p for p in renamed), str(renamed))

    retyped = validate_expansion(original, _expanded(
        {"## 3. 谱域重标定\n\ntype: method": "## 3. 谱域重标定\n\ntype: results"}))
    check("改 type 被拦", any("type 被改" in p for p in retyped), str(retyped))

    dropped = validate_expansion(original, _expanded(
        {"## 2. Introduction\n\ntype: intro\n\n### 2.1 背景与问题\n"
         "- 领域现状与现有方法的不足\n\n": ""}))
    check("丢章被拦", any("丢了" in p for p in dropped), str(dropped))
    check("丢章同时报出章节数变化",
          any("章节数变了" in p for p in dropped), str(dropped))

    added = validate_expansion(original, _expanded(
        {"## 4. Results": "## 5. Discussion\n\ntype: discussion\n\n"
                          "### 5.1 讨论\n- 要点\n\n## 4. Results"}))
    check("加章被拦", any("多出了章节编号" in p for p in added), str(added))

    empty = validate_expansion(original, _expanded(
        {"### 4.1 主结果\n- 只用结果库里的真实数字\n": ""}))
    check("某章没展开出小节被拦",
          any("没展开出小节" in p for p in empty), str(empty))


# ── 短章：合成小节渲染与校验 ──────────────────────────────────────────
def _expanded_with_short() -> list[dict]:
    """一份含短章（要点挂章下、合成小节）的展开结果。不动共享 _expanded()，保其
    sum(len(sections))==5 断言。"""
    text = """# expanded

## 1. Abstract

type: abstract

- 一句问题一句方法一个 headline 数

## 2. Introduction

type: intro

### 2.1 背景与问题
- 领域现状与现有方法的不足

## 3. 谱域重标定

type: method

### 3.1 总体框架
- 输入输出与数据流
### 3.2 频域重标定算子
- 从 idea.md 第 4 节展开机制

## 4. Results

type: results

### 4.1 主结果
- 只用结果库里的真实数字
"""
    return parse_outline(write_outline(text))


def test_render_outline_short_chapter_no_h3():
    """合成小节渲染时不写 ### 行，要点直接挂在 ## 章下；往返保住要点。"""
    print("\n[短章渲染：无 ### 行]")
    expanded = _expanded_with_short()
    text = render_outline(expanded, "X")
    # abstract 章下不应出现 ### 行
    abstr_block = text.split("## 1. Abstract", 1)[1].split("## 2.", 1)[0]
    check("短章渲染无 ### 行", "###" not in abstr_block, abstr_block)
    check("短章要点直接挂在 ## 下", "- 一句问题一句方法一个 headline 数" in abstr_block)
    # 往返：重解析后合成小节要点保留
    reparsed = parse_outline(write_outline(text))
    abstract = reparsed[0]
    check("往返后 abstract 仍 1 个合成小节",
          len(abstract["sections"]) == 1 and abstract["sections"][0].get("synthetic") is True,
          str(abstract["sections"]))
    check("往返后合成小节要点保留",
          abstract["sections"][0]["bullets"] == ["一句问题一句方法一个 headline 数"],
          str(abstract["sections"][0]["bullets"]))


def test_render_outline_preserves_chapter_words():
    """短章 ## 标题带字数 → render 在 ## 行保留 (~N words)，重解析 target_words 不变。"""
    print("\n[短章章标题字数往返]")
    chapters = parse_outline(write_outline(
        "## 1. Abstract (~300 words)\n\ntype: abstract\n\n- 一句问题\n"))
    check("解析章标题字数 → 合成小节 300",
          chapters[0]["sections"][0]["target_words"] == 300)
    text = render_outline(chapters, "X")
    check("render 在 ## 标题行保留字数", "## 1. Abstract (~300 words)" in text, text)
    reparsed = parse_outline(write_outline(text))
    check("往返后 target_words=300",
          reparsed[0]["sections"][0]["target_words"] == 300)


def test_short_chapter_validation_passes():
    """短章合成小节形态通过 validate_expansion。"""
    print("\n[短章校验放行]")
    original = parse_outline(write_outline(SKELETON))  # 4 章裸骨架，含 abstract
    expanded = _expanded_with_short()
    problems = validate_expansion(original, expanded)
    check("短章形态校验无问题", problems == [], str(problems))


def test_prompt_includes_short_chapter_bullets():
    """作者已有短章要点必须进 expand prompt（否则 Manager 看不到却被要求保留）。"""
    print("\n[短章要点进 prompt]")
    chapters = parse_outline(write_outline(
        "## 1. Abstract (~300 words)\n\ntype: abstract\n\n- 作者写的要点\n"))
    prompt = build_expand_prompt(chapters, "/tmp/o.md", "/tmp/o.zh.md", "/tmp/idea.md", 1)
    check("短章要点出现在 prompt", "作者写的要点" in prompt)
    check("短章章标题字数出现在 prompt", "(~300 words)" in prompt)
    check("骨架标注'作者已写章级要点'", "作者已写章级要点" in prompt)


def test_short_chapter_extra_h3_rejected():
    """原本无 ### 的短章，Manager 新增 ### 必须被校验拦下。"""
    print("\n[短章禁止新增 ###]")
    # 原始：abstract 裸骨架（无要点、无小节）
    original = parse_outline(write_outline("## 1. Abstract\n\ntype: abstract\n"))
    # Manager 错误地给短章写了 ### 小节
    expanded = parse_outline(write_outline(
        "## 1. Abstract\n\ntype: abstract\n\n### 1.1 摘要\n- 要点\n"))
    problems = validate_expansion(original, expanded)
    check("短章新增 ### 被拦",
          any("短章" in p and "不能新增 ###" in p for p in problems), str(problems))

    # 反例：作者原本就写了真实 ### 的短章仍允许（尊重作者显式结构）
    original_with_real = parse_outline(write_outline(
        "## 1. Abstract\n\ntype: abstract\n\n### 1.1 摘要 (~150 words)\n- 原\n"))
    expanded_with_real = parse_outline(write_outline(
        "## 1. Abstract\n\ntype: abstract\n\n### 1.1 摘要 (~150 words)\n- 原\n- 补\n"))
    check("作者原本写了 ### 的短章允许补要点",
          validate_expansion(original_with_real, expanded_with_real) == [],
          str(validate_expansion(original_with_real, expanded_with_real)))


def test_render_round_trip():
    """重新序列化后必须还能解析回同一结构 —— 落盘的是这份,不是模型原文。"""
    print("\n[重新序列化]")
    expanded = _expanded()
    text = render_outline(expanded, "Paper outline — X", EXPANDED_MARKER)
    check("首行是展开标记", text.splitlines()[0] == EXPANDED_MARKER)
    check("不夹带说明文字", "语法" not in text and "词表" not in text)
    reparsed = parse_outline(write_outline(text))
    check("往返后结构不变",
          outline_skeleton_digest(reparsed) == outline_skeleton_digest(expanded))
    check("往返后小节标题不变",
          [s["title"] for c in reparsed for s in c["sections"]]
          == [s["title"] for c in expanded for s in c["sections"]])
    check("往返后要点不变",
          [s["bullets"] for c in reparsed for s in c["sections"]]
          == [s["bullets"] for c in expanded for s in c["sections"]])
    check("展开产物不含字数标注", "(~" not in text)

    # 作者补了字数之后,往返必须保住它 —— 否则 --init 拿到的还是默认 250。
    with_words = _expanded({"### 3.2 频域重标定算子": "### 3.2 频域重标定算子 (~350 words)"})
    again = parse_outline(write_outline(render_outline(with_words, "X")))
    got = [s["target_words"] for c in again for s in c["sections"]]
    check("作者补的字数在往返中保留", 350 in got, str(got))


def test_section_level_type_survives():
    """小节级 `- type:` 覆盖是既有功能,展开链路不能把它吃掉。"""
    print("\n[小节级 type 覆盖]")
    expanded = _expanded({"### 4.1 主结果\n": "### 4.1 主结果\n- type: results\n"})
    text = render_outline(expanded, "X")
    check("序列化写出小节级 type", "- type: results" in text)
    reparsed = parse_outline(write_outline(text))
    section = reparsed[3]["sections"][0]
    check("往返后小节级 type 保留", section["type"] == "results", section["type"])
    check("小节级 type 没被当成要点",
          "type: results" not in " ".join(section["bullets"]))


# ── 作者已手写的小节不许被动 ──────────────────────────────────────────
def _one(section: dict) -> list[dict]:
    return [{"number": 4, "title": "Results", "type": "results",
             "sections": [section]}]


def _sec(title="主结果", bullets=("只用结果库的真实数字",), words=300,
         explicit=True, stype="ablation", number=1) -> dict:
    return {"number": number, "title": title, "target_words": words,
            "bullets": list(bullets), "words_explicit": explicit,
            "type": stype}


def test_authors_own_sections_are_protected():
    """作者手写过小节的章:改名/改字数/改 type/删要点都必须被拦。

    提示词里写了「KEEP their titles and order verbatim」,但那只是请求。作者手写
    小节意味着他对这一章已有明确设计,被静默改掉时展开产物看上去完全正常。
    """
    print("\n[作者已有小节的保护]")
    base = _one(_sec())

    cases = [
        ("小节被改名", _one(_sec(title="实验结果", bullets=("随便写",))), "改名或重排"),
        ("字数标注被抹掉", _one(_sec(words=250, explicit=False)), "字数标注被抹掉"),
        ("字数被改", _one(_sec(words=500)), "字数被改"),
        ("小节 type 被改", _one(_sec(stype="")), "type 被改"),
        ("作者的要点被删", _one(_sec(bullets=("换成别的",))), "删掉了作者写的要点"),
    ]
    for label, tampered, expect in cases:
        problems = validate_expansion(base, tampered)
        check(f"{label}被拦住", any(expect in p for p in problems),
              str(problems))

    # 小节整个消失:两条都该报(没展开出小节 + 已有小节数变少)
    problems = validate_expansion(base, [{"number": 4, "title": "Results",
                                          "type": "results", "sections": []}])
    check("小节被整个删掉被拦住",
          any("没展开出小节" in p for p in problems)
          and any("只剩 0 个" in p for p in problems), str(problems))

    # 合法操作必须放行,否则 Manager 什么都干不了
    print("  -- 合法操作放行 --")
    check("往作者的小节里加要点 → 放行",
          validate_expansion(base, _one(_sec(
              bullets=("只用结果库的真实数字", "补一条边界约束")))) == [])
    check("在作者的小节后面新增小节 → 放行",
          validate_expansion(base, [{
              "number": 4, "title": "Results", "type": "results",
              "sections": [_sec(), _sec(title="消融", explicit=False,
                                        stype="", number=2)]}]) == [])
    check("作者没写小节的章 → Manager 自由拆分",
          validate_expansion(
              [{"number": 4, "title": "R", "type": "results", "sections": []}],
              [{"number": 4, "title": "R", "type": "results",
                "sections": [_sec(number=1), _sec(title="别的", number=2)]}]) == [])


def test_chapter_reordering_is_caught():
    """章节被重排必须报错——章序决定文件夹的 NN- 前缀与整篇运行顺序。"""
    print("\n[章节重排]")
    mk = lambda n, t, ty, secs: {"number": n, "title": t, "type": ty,
                                 "sections": secs}
    original = [mk(1, "Abstract", "abstract", []), mk(2, "Method", "method", [])]
    reordered = [mk(2, "Method", "method", [_sec(title="A", explicit=False)]),
                 mk(1, "Abstract", "abstract", [_sec(title="B", explicit=False)])]
    problems = validate_expansion(original, reordered)
    check("重排被拦住", any("章节顺序变了" in p for p in problems), str(problems))
    check("报出了前后顺序", "[1, 2]" in " ".join(problems)
          and "[2, 1]" in " ".join(problems), str(problems))


# ── 空章拦截(--init 在生成工作区之前的判据)──────────────────────────
def test_bare_chapters_are_detected():
    """只填了表格骨架的 outline:每章都没小节,必须被识别出来。

    这是最容易踩的一步 —— 章标题与 type 都对,--init 照样能生成 brief,但那份
    brief 让起草退回单段、要点为空,而流水线全程显示成功。
    """
    skeleton = parse_outline(write_outline(
        "# T\n\n| # | 章标题 | type |\n|---|---|---|\n"
        "| 1 | Abstract | abstract |\n| 2 | Method | method |\n"))
    bare = chapters_without_sections(skeleton)
    check("表格骨架的每一章都被判为空章", len(bare) == 2, str(len(bare)))
    check("返回的是章节对象而非名字",
          bare[0]["title"] == "Abstract", str(bare[0].get("title")))

    # 展开之后不该再报空章,否则 --init 永远放行不了
    check("展开后没有空章", chapters_without_sections(_expanded()) == [],
          str([c["title"] for c in chapters_without_sections(_expanded())]))

    # 只有部分章展开的中间状态:必须只报那些没展开的
    partial = parse_outline(write_outline(
        render_outline(_expanded(), "T") + "\n## 9. Appendix\n\ntype: background\n"))
    names = [c["title"] for c in chapters_without_sections(partial)]
    check("半展开时只报没小节的那一章", names == ["Appendix"], str(names))


if __name__ == "__main__":
    test_skeleton_parses_without_sections()
    test_prompt_states_the_hard_constraints()
    test_prompt_preserves_authors_existing_sections()
    test_valid_expansion_passes()
    test_structural_tampering_is_caught()
    test_render_round_trip()
    test_section_level_type_survives()
    test_authors_own_sections_are_protected()
    test_chapter_reordering_is_caught()
    test_bare_chapters_are_detected()
    test_render_outline_short_chapter_no_h3()
    test_render_outline_preserves_chapter_words()
    test_short_chapter_validation_passes()
    test_prompt_includes_short_chapter_bullets()
    test_short_chapter_extra_h3_rejected()

    print(f"\n{'='*56}")
    if FAILURES:
        print(f"EXPAND TESTS FAILED — {len(FAILURES)}/{CHECKS} 项未通过")
        for name in FAILURES:
            print(f"  · {name}")
        sys.exit(1)
    print(f"EXPAND TESTS PASSED — {CHECKS} 项检查全过")
