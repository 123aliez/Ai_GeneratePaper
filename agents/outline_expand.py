"""Outline 展开:章节骨架 → 带小节与要点的完整 outline。

分工的理由。`outline.md` 里有两类信息,该由不同的人来定:

* **论文的结构** —— 分几章、每章叫什么、什么 `type:`、什么顺序。这是作者对
  自己工作的判断,框架不替你决定,也没有默认模板。
* **每章拆成哪几个小节、每节讲什么** —— 这是从「创新点」推导出的施工细节。
  作者写得出来,但那正是 Manager 该干的活:它读 `idea.md`,知道方法有几个
  模块、哪个模块需要单独一节、哪些内容属于邻章。

所以流程是 `骨架(你写) → --expand(Manager 补) → 你审 → --init`。展开产物写到
`outline.expanded.md` 而不是原地覆盖:Manager 的判断需要你过一遍再采纳。

字数不由 Manager 定 —— 篇幅是投稿约束(会议页数、章节配比),只有作者知道。
不带 `(~N words)` 的小节,框架按该小节 type 查 DEFAULT_WORDS_BY_TYPE 给经验默认值
(小节级 type 优先 → 章 type),都查不到才退回 DEFAULT_SECTION_WORDS=400。
"""
import re
from pathlib import Path

from .outline import (
    DEFAULT_SECTION_WORDS, OUTLINE_PATH, OutlineRouteError, SHORT_CHAPTER_TYPES,
    parse_outline,
)
from .chapter_type import DEFAULT_TYPE

EXPANDED_PATH_NAME = "outline.expanded.md"

# 中英双轨:英文版喂给 Agent,中文版给作者看/改。--expand 一次生成两份。
# 输入是用户填的 outline_draft.md(表格骨架),输出直接是 outline.md(定稿,--init 用)
# 与 outline.zh.md(中文对照版)。改名/保留即定稿,不需要额外 rename。
EN_EXPANDED_PATH_NAME = "outline.md"          # 英文版:喂给 Agent(--init 的输入)
ZH_EXPANDED_PATH_NAME = "outline.zh.md"       # 中文版:作者日常看/改

# 展开产物的首行标记。用途是让 --init 能分辨"这是 --expand 的产物,作者可能还
# 没审过",在生成工作区之前提醒一句。
EXPANDED_MARKER = "<!-- outline-expanded: 由 --expand 生成,请审阅后改名为 outline.md -->"
EN_EXPANDED_MARKER = "<!-- outline-expanded-en: 英文版,喂给 Agent; --init 的输入 -->"
ZH_EXPANDED_MARKER = "<!-- outline-expanded-zh: 中文版,给作者审阅;结构与英文版逐字一致,仅语言不同 -->"


def outline_skeleton_digest(chapters: list[dict]) -> str:
    """骨架摘要:章号 + 标题 + type。展开前后必须逐字一致,用它来比对。"""
    return "\n".join(f"{c['number']}|{c['title']}|{c['type']}" for c in chapters)


def render_outline(chapters: list[dict], title: str = "",
                   marker: str = "") -> str:
    """把章节清单序列化回 outline.md 的语法。

    只写结构信息(章标题 / type / 小节 / 要点),不写任何说明文字 —— 这份产物是
    给 `--init` 吃的,也是给作者审的,夹带模板注释只会让人分不清哪句是自己写的。
    """
    lines = []
    if marker:
        lines.append(marker)
    lines.append(f"# {title or 'Paper outline'}")
    lines.append("")
    for chapter in chapters:
        # 短章可能带章级字数（## 1. Abstract (~300 words)）：若它被合成单段小节且该小节
        # words_explicit，把字数补回 ## 标题行，保证往返不丢作者钉的字数。
        synth_words = None
        if len(chapter["sections"]) == 1 and chapter["sections"][0].get("synthetic") \
                and chapter["sections"][0].get("words_explicit"):
            synth_words = chapter["sections"][0]["target_words"]
        title_suffix = f" (~{synth_words} words)" if synth_words else ""
        lines.append(f"## {chapter['number']}. {chapter['title']}{title_suffix}")
        lines.append("")
        lines.append(f"type: {chapter['type']}")
        lines.append("")
        for section in chapter["sections"]:
            if section.get("synthetic"):
                # 合成小节：不写 ### 行，要点直接挂在 ## 章下（保持短章形状往返）。
                if section.get("type"):
                    lines.append(f"- type: {section['type']}")
                for bullet in section["bullets"]:
                    lines.append(f"- {bullet}")
                lines.append("")
                continue
            # 只写作者显式标过的字数。把解析时填进去的默认值写回文件,等于
            # 让作者以为 250 是自己定的,而这份文件下一步就要变成 outline.md。
            words = (section.get("target_words")
                     if section.get("words_explicit") else None)
            suffix = f" (~{words} words)" if words else ""
            lines.append(f"### {chapter['number']}.{section['number']} "
                         f"{section['title']}{suffix}")
            if section.get("type"):
                lines.append(f"- type: {section['type']}")
            for bullet in section["bullets"]:
                lines.append(f"- {bullet}")
            lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def skeleton_report(chapters: list[dict]) -> list[str]:
    """展开前打印的骨架清单,让作者确认 Manager 将要按什么结构展开。"""
    lines = []
    for chapter in chapters:
        count = len(chapter["sections"])
        has = f"已有 {count} 个小节" if count else "无小节,待展开"
        flag = "  ← type 无法识别" if chapter["type"] == DEFAULT_TYPE else ""
        bad = [sec["title"] for sec in chapter["sections"] if sec.get("type_unrecognized")]
        if bad:
            flag += f"  ← 小节 type 无法识别: {', '.join(bad)}"
        lines.append(f"  {chapter['number']:>2}. {chapter['title']:<28} "
                     f"type={chapter['type']:<12} {has}{flag}")
    return lines


def budget_clause(chapters: list[dict]) -> str:
    """篇幅预算段:有 paper.toml 的 pages 才有。给规划者一张每章建议字数表作起点。"""
    from .outline import page_budget, plan_chapter_words
    budget = page_budget()
    if not budget:
        return ""
    pages, per_page = budget
    plan = plan_chapter_words(chapters, pages, per_page)
    rows = "\n".join(
        f"  {c['number']}. {c['title']} ({c['type']}): about {plan[c['number']]} words "
        f"(~{plan[c['number']] / per_page:.1f} pages)" for c in chapters)
    return (
        f"## Page budget (you allocate it)\n"
        f"The paper body is capped at about {pages} pages ({pages * per_page} words at "
        f"{per_page} words per page, references excluded). Suggested allocation per chapter, "
        f"from conference conventions:\n{rows}\n"
        f"You decide the final split. Write `(~N words)` on EVERY `###` subsection you create "
        f"(multiples of 25), and for a short chapter (no `###`) on its `##` heading line. Rules:\n"
        f"- Allocate by how much real material the chapter has: the idea document and data "
        f"index decide it. Core content (the method, the main results) gets the most words; a "
        f"chapter with thin material gets FEWER words than the suggestion. Never pad.\n"
        f"- Stay within about 50% of each chapter's suggestion unless the material clearly "
        f"calls for more or less; chapter totals must add up to no more than {pages * per_page} "
        f"words.\n"
        f"- Split a chapter's words across its subsections by the weight of their content.\n\n")


def build_expand_prompt(chapters: list[dict], out_en_path: str,
                        out_zh_path: str, idea_path: str, total: int) -> str:
    """Manager 的展开任务。

    刻意收紧到两件事:拆小节、写要点。不许改章节结构 —— 那是作者的设计,而
    Manager 手里只有 `idea.md`,没有投稿目标、没有篇幅预算、也不知道作者为什么
    把某两章分开写。越权改结构会让作者以为自己的设计被采纳了,实际没有。

    中英双轨:让 Manager 对**同一份章节结构**写两份文件 —— 英文版 outline.md
    (Agent 吃的结构)与中文版 outline.zh.md(作者看/改)。英文版是初始版,作者改完
    中文后由 --init 时翻译覆盖英文版。结构、type、小节划分两份逐字一致,仅语言不同。
    """
    skeleton_lines = []
    existing = []
    for chapter in chapters:
        real_sections = [s for s in chapter["sections"] if not s.get("synthetic")]
        synthetic = next((s for s in chapter["sections"] if s.get("synthetic")), None)
        if real_sections:
            status = f", 作者已写了 {len(real_sections)} 个小节,保留并在其基础上补要点"
        elif synthetic:
            status = ", 作者已写章级要点,逐字保留并可补充"
        else:
            status = ", 无小节"
        skeleton_lines.append(
            f"{chapter['number']}. {chapter['title']}  (type: {chapter['type']}{status})")

        # 短章作者已有章级要点：必须原样塞进 prompt，否则 Manager 看不到、却又被
        # _section_problems 要求逐字保留，必然校验失败或丢内容。以"章级内容块"呈现，
        # 明确告诉 Manager 这些要点挂在 ## 下、不要挪到 ###。
        if synthetic:
            words = (f" (~{synthetic['target_words']} words)"
                     if synthetic.get("words_explicit") else "")
            existing.append(
                f"\n### 第 {chapter['number']} 章 作者已写的短章要点"
                f"(逐字保留,直接挂在 ## 下,不要改成 ### 小节)")
            existing.append(f"## {chapter['number']}. {chapter['title']}{words}")
            existing.append(f"type: {chapter['type']}")
            existing += [f"- {bullet}" for bullet in synthetic["bullets"]]
            continue
        if not real_sections:
            continue
        # 按 outline 的原始语法给,不做摘要式改写。作者写的字数和小节级 type 也是
        # 他的决定,校验会核对它们有没有被动 —— 那就必须先让 Manager 看见。
        existing.append(f"\n### 第 {chapter['number']} 章 作者已有的小节"
                        f"(标题、字数、type 必须逐字保留)")
        for section in real_sections:
            words = (f" (~{section['target_words']} words)"
                     if section.get("words_explicit") else "")
            existing.append(f"### {chapter['number']}.{section['number']} "
                            f"{section['title']}{words}")
            if section.get("type"):
                existing.append(f"- type: {section['type']}")
            existing += [f"- {bullet}" for bullet in section["bullets"]]
    skeleton = "\n".join(skeleton_lines)
    existing_block = "\n".join(existing) if existing else ""

    short_list = ", ".join(sorted(SHORT_CHAPTER_TYPES))

    return (
        f"You are planning the section-level structure of a paper outline.\n\n"
        f"The document idea.md (attached) states the author's contribution: the core "
        f"insight, the method design, the delta against prior work, and the claim "
        f"list. Everything you write must be derivable from it.\n\n"
        f"The author has fixed the chapter structure. It is NOT yours to change:\n\n"
        f"{skeleton}\n"
        f"{existing_block}\n\n"
        f"## Your task\n"
        f"For each of the {total} chapters, decide its `###` subsections and write "
        f"the drafting bullets under each one. Nothing else.\n\n"
        f"## Hard constraints\n"
        f"- Do NOT add, remove, merge, split, reorder or rename a chapter.\n"
        f"- Do NOT change any chapter's `type:` line.\n"
        f"- Word counts: where the author already wrote `(~N words)`, reproduce it verbatim. "
        f"For subsections you create, follow the Page budget section when present; without "
        f"one, omit the annotation (length is the author's call).\n"
        f"- Where the author already wrote subsections, KEEP their titles and order "
        f"verbatim; you may only add bullets to them.\n"
        f"- Give each chapter 2 to 4 subsections. A chapter with 3+ subsections is "
        f"drafted in three separate passes, so each subsection must be a coherent "
        f"unit of writing, not an arbitrary slice.\n"
        f"- EXCEPTION — short chapter types ({short_list}) are drafted as a SINGLE "
        f"section: do NOT write any `###` subsection for them. Instead write 2 to 4 "
        f"bullets directly under the chapter's `##` heading (the chapter's own "
        f"bullets). The framework synthesises a single drafting section from those "
        f"chapter-level bullets. If the author already wrote chapter-level bullets "
        f"for a short chapter, KEEP them verbatim.\n"
        f"- Bullets not under a `###` subsection are allowed ONLY for the short "
        f"chapter types above. For every other chapter, place every bullet under a "
        f"`###` subsection.\n"
        f"- Subsection type: the FIRST line under every `###` subsection you create is "
        f"`- type: X`, with X one of: abstract, intro, related, background, method, theory, "
        f"experiments, results, analysis, ablation, discussion, limitations, conclusion. It "
        f"decides which writing and review rules that subsection gets. Use the chapter's own "
        f"type unless the subsection's content clearly belongs to another one — typically an "
        f"`experiments` chapter that also holds the main comparison (results), an ablation "
        f"(ablation) or a further analysis (analysis). Subsections the author already wrote "
        f"keep their `- type:` line exactly as given (or none, if none was given). Short "
        f"chapters get no type line beyond the chapter's own.\n\n"
        f"{budget_clause(chapters)}"
        f"## Length & structure heuristics (how to slice chapters, venue-style)\n"
        f"- Prioritise the parts most readers actually read: Abstract + Introduction "
        f"+ the headline figure. Make those subsections crisp.\n"
        f"- Method and Experiments/Results are the content core — they should get "
        f"the most subsections and the most bullets. Ablation/Analysis subsections "
        f"add a lot of persuasiveness; include them when the idea supports it.\n"
        f"- Tune the balance to the paper kind: a theory-heavy paper puts more in "
        f"Method/Theory and less in experiments; a pure empirical/systems paper "
        f"does the reverse.\n"
        f"- Related Work can sit after Intro or near the Conclusion — your call; do "
        f"not force one placement.\n"
        f"- Venue-mandated sections: an ACL-style paper MUST have a standalone "
        f"Limitations chapter (it does not count against the main page limit); some "
        f"venues also want Broader Impact / Ethics. If the author's chapter list "
        f"lacks these for such a venue, do NOT add chapters (structure is fixed) — "
        f"but a Limitations subsection inside the right chapter is fine.\n"
        f"- Appendices hold hyperparameters, proofs, extra experiments; keep the main "
        f"text self-contained. You are planning the MAIN text only.\n"
        f"- Without a Page budget section, you do not write word counts: the framework fills "
        f"a per-type default and the author overrides the important ones.\n\n"
        f"## What a good bullet looks like\n"
        f"Bullets are instructions handed verbatim to the drafting agent. Each "
        f"subsection needs 2 to 5 of them, covering:\n"
        f"- WHAT to write, anchored to a specific part of the idea document "
        f"(\"expand the spectral decomposition from idea.md section 4: why low "
        f"frequency bands carry class identity\"), never a vague restatement of "
        f"the title.\n"
        f"- WHICH BOUNDARY not to cross — the single most valuable line you can "
        f"write. Say what belongs to a neighbouring chapter and must be left out "
        f"(\"state the mechanism only; every measured number belongs to the "
        f"results chapter\"). Cross-chapter duplication and scope creep are the "
        f"most common failure of this pipeline and the drafter cannot judge "
        f"boundaries on its own.\n"
        f"- WHAT TO MARK when the subsection must state something the idea document "
        f"does not supply: [DESIGN DETAIL NEEDED] for a missing mechanism, [MISSING DATA] "
        f"for a number, [CITATION NEEDED] for an unsupported claim. Never invent the "
        f"content instead, and do not add bullets whose only job is to mark an absence.\n\n"
        f"## One owner per statement (this prevents the paper from repeating itself)\n"
        f"Chapters are drafted separately and each drafter only follows its own bullets, so YOU "
        f"are the only place where repetition can be prevented. Before writing bullets, decide "
        f"for every recurring statement which single subsection OWNS it, and write it only "
        f"there:\n"
        f"- limitations and scope limits belong ONLY to the last chapter (Conclusion, or a "
        f"Limitations chapter), as one short paragraph; no other subsection gets a bullet "
        f"about limits, caveats, provenance disclaimers or what the work does not do;\n"
        f"- do not plan subsections or bullets about what was NOT done, measured or compared "
        f"(no 'reproducibility status', 'limits of the evidence', 'mark unavailable values'). "
        f"The paper presents what the work did and achieved, affirmatively;\n"
        f"- a source attribution that idea.md requires (e.g. results taken from a cited "
        f"paper) is stated once, where the results are introduced, as a plain citation;\n"
        f"- a definition (a term, a metric, a notation) is explained once, in the subsection that introduces it; later bullets say "
        f"'use the term as defined in X', never 'explain again'.\n"
        f"Bullets must not say 'make clear that ...' about a statement owned elsewhere. "
        f"When the data index or experiment settings are attached, they are the source of the "
        f"numbers and the setup: point the data chapters at them and never write that the "
        f"values are unavailable.\n\n"
        f"## Chapter shapes that top venues expect\n"
        f"- Introduction: first subsection opens with a concrete problem (a specific example or "
        f"phenomenon, not 'X is very important'); the LAST subsection of the introduction is the "
        f"contribution list: 2 to 4 specific, falsifiable contributions, each pointing forward to "
        f"the chapter that supports it. Each contribution is 2 to 3 sentences (what is done, "
        f"where the evidence is) and never restates a point already made in the same list. No bullet about a paper roadmap ('the rest of this "
        f"paper'); an introduction stays around one page.\n"
        f"- Abstract: problem, gap, what we introduce, ONE headline result sentence with at most two "
        f"numbers (data-index placeholders) that summarizes all compared settings, meaning. "
        f"Nothing else.\n"
        f"- Results: one subsection per claim; each opens with the finding, then the numbers, "
        f"then what it means.\n\n"
        f"Do not write bullets about chapter transitions or terminology "
        f"consistency. The pipeline injects the writing contract and freezes a "
        f"notation table on its own; bullets about them are noise.\n\n"
        f"## Output\n"
        f"Return the SAME chapter structure as TWO files, each in its own file block, in exactly this "
        f"syntax and nothing else inside the blocks — no preamble, no explanation, no code fences:\n\n"
        f"1. {Path(out_en_path).name} — ENGLISH version. Chapter titles, subsection titles, and "
        f"bullets all in English. This is the initial structure the drafting agent reads.\n"
        f"2. {Path(out_zh_path).name} — CHINESE version. Subsection titles and bullets in Chinese "
        f"(match the idea document's language). CHAPTER titles stay EXACTLY as the author wrote "
        f"them in the skeleton (both files, verbatim) — they name the chapter folders. This is what the "
        f"author reviews and edits; when the author edits it, the English version is "
        f"re-translated to match.\n\n"
        f"The two files must be structurally IDENTICAL: same chapters, same order, same "
        f"`type:` values, same subsections and their boundaries. Only the language of "
        f"titles and bullets differs. Reproduce every chapter in the author's order with "
        f"the author's exact title and `type:` value in BOTH files. Keep author-written "
        f"subsections (titles, order, word counts, per-section type) verbatim in BOTH "
        f"files. Return BOTH files.\n\n"
        f"Syntax (used identically in both files, English shown here):\n\n"
        f"## 4. Method\n\n"
        f"type: method\n\n"
        f"### 4.1 <subsection title>\n"
        f"- <bullet>\n"
        f"- <bullet>\n\n"
        f"### 4.2 <subsection title>\n"
        f"- <bullet>\n"
    )


def build_translate_prompt(out_zh_path: str, out_en_path: str, old_en_path: str = "") -> str:
    """Manager 把作者改好的中文版 outline.zh.md 翻译成英文版 outline.md。

    只翻译语言,不动结构:章、type、小节划分、字数、小节级 type 全部逐字保留。
    这是 --init 的前置步骤——作者只维护中文,Agent 吃英文,翻译由 Manager 完成。
    """
    return (
        f"You are translating a paper outline from Chinese to English.\n\n"
        f"The document outline.zh.md (attached) is the author-reviewed Chinese outline.\n"
        f"Translate it into English and return the result as {Path(out_en_path).name}.\n\n"
        f"Hard rules:\n"
        f"- Keep the STRUCTURE byte-identical: same chapters, same order, same "
        f"`type:` values, same subsections and their boundaries.\n"
        f"- Translate chapter titles, subsection titles, and bullets to English.\n"
        f"- Keep `(~N words)` word counts and per-section `- type: X` lines VERBATIM "
        f"(do not translate `type` values or numbers).\n"
        f"- Keep the first-line HTML comment marker as-is.\n"
        + (f"- The document outline.previous.md (attached) is the previous English outline. For every "
           f"chapter title, subsection title and bullet whose Chinese text did not change, REUSE its "
           f"previous English wording exactly (chapter folder names are derived from the English "
           f"titles — unchanged chapters must keep their titles). Translate only what the author "
           f"changed.\n" if old_en_path else "") +
        f"- Return ONLY {Path(out_en_path).name}, in exactly the outline syntax "
        f"(## chapter / type: / ### section / - bullet). No preamble or explanation.\n"
    )


def _section_problems(original: dict, expanded: dict) -> list[str]:
    """作者**已经手写过小节**的那一章:核对它们没被动。

    提示词里写的是「KEEP their titles and order verbatim」,但那只是请求。作者手写
    小节意味着他对这一章已经有明确设计(常见的是给 Results 定死了 `(~300 words)`
    和一条 `- type: ablation`),Manager 改名、重排、删要点或抹掉字数,那些决定就
    静默消失了 —— 而展开产物看上去完全正常。
    """
    want = original["sections"]
    if not want:
        return []          # 作者没写小节,Manager 怎么拆都是它的活

    got = expanded["sections"]
    prefix = f"第 {original['number']} 章"
    if len(got) < len(want):
        return [f"{prefix}作者已有 {len(want)} 个小节,展开后只剩 {len(got)} 个"]

    problems = []
    for index, (wanted, actual) in enumerate(zip(want, got), start=1):
        if actual["title"].strip() != wanted["title"].strip():
            # 只报第一处错位:标题一旦对不上,后面每个位置都会连带报错,那种
            # 输出看不出真正改了什么。
            problems.append(
                f"{prefix}第 {index} 个小节被改名或重排:"
                f"'{wanted['title']}' → '{actual['title']}'")
            break
        if wanted.get("words_explicit") and not actual.get("words_explicit"):
            problems.append(
                f"{prefix}小节 '{wanted['title']}' 的字数标注被抹掉"
                f"(原为 ~{wanted['target_words']} words)")
        elif actual["target_words"] != wanted["target_words"]:
            problems.append(
                f"{prefix}小节 '{wanted['title']}' 的字数被改:"
                f"{wanted['target_words']} → {actual['target_words']}")
        if (wanted.get("type") or "") != (actual.get("type") or ""):
            problems.append(
                f"{prefix}小节 '{wanted['title']}' 的 type 被改:"
                f"{wanted.get('type') or '(无)'} → {actual.get('type') or '(无)'}")
        missing = [b for b in wanted["bullets"] if b not in actual["bullets"]]
        if missing:
            problems.append(
                f"{prefix}小节 '{wanted['title']}' 删掉了作者写的要点:{missing}")
    return problems


def validate_expansion(original: list[dict], expanded: list[dict]) -> list[str]:
    """校验 Manager 没越权改结构。返回问题列表,空列表表示通过。

    必须校验而不是信任:章节结构是作者的设计,Manager 悄悄合并两章、改掉一个
    `type:`,而 `--init` 照样生成工作区 —— 作者要到看见文件夹名不对才发现,
    那时候 token 已经花了。
    """
    problems = []
    if len(expanded) != len(original):
        problems.append(
            f"章节数变了:骨架 {len(original)} 章,展开后 {len(expanded)} 章")

    # 顺序单独比一次。下面按章号建字典查,那样重排看不出来 —— 而章序决定文件夹的
    # NN- 前缀,也就决定整篇模式的运行顺序和每章的邻章,不能让它被悄悄换掉。
    want_order = [c["number"] for c in original]
    got_order = [c["number"] for c in expanded]
    if got_order != want_order:
        problems.append(f"章节顺序变了:{want_order} → {got_order}")

    by_number = {c["number"]: c for c in expanded}
    for chapter in original:
        got = by_number.get(chapter["number"])
        if got is None:
            problems.append(f"第 {chapter['number']} 章 '{chapter['title']}' 丢了")
            continue
        if got["title"].strip() != chapter["title"].strip():
            problems.append(
                f"第 {chapter['number']} 章标题被改:"
                f"'{chapter['title']}' → '{got['title']}'")
        if got["type"] != chapter["type"]:
            problems.append(
                f"第 {chapter['number']} 章 type 被改:"
                f"{chapter['type']} → {got['type']}")
        # 短章合法形态是"章下直接挂要点、无 ### 小节"（合成单段小节）。非短章若无小节
        # 则是 Manager 没干活的硬错。这里只对非短章报"没展开出小节"；空短章（连章级
        # 要点都没有 → 无合成小节）由 --init 的 chapters_without_sections 兜底拦下。
        if not got["sections"] and got["type"] not in SHORT_CHAPTER_TYPES:
            problems.append(
                f"第 {chapter['number']} 章 '{chapter['title']}' 没展开出小节")
        # 原本没有真实 ### 小节的短章（作者用新风格：要点挂 ## 下），Manager 不得新增
        # ### 小节——否则把单段短章拆开了，与短章"单段起草"的设计相悖。作者原本就写了
        # 真实 ### 的短章仍允许（尊重作者显式结构）。
        original_has_real = any(not s.get("synthetic") for s in chapter["sections"])
        if (got["type"] in SHORT_CHAPTER_TYPES and not original_has_real
                and got["sections"]
                and not (len(got["sections"]) == 1
                         and got["sections"][0].get("synthetic"))):
            problems.append(
                f"第 {chapter['number']} 章 '{chapter['title']}' 是短章,"
                f"应把要点直接写在 ## 下,不能新增 ### 小节")
        problems += _section_problems(chapter, got)
        # 规划者给小节标的 type 必须是标准类型之一:写错的 type 解析时会被忽略,
        # 那个小节就悄悄退回章 type,作者审大纲时看见的 type 和实际生效的不一致。
        for section in got["sections"]:
            if section.get("type_unrecognized"):
                problems.append(
                    f"第 {chapter['number']} 章小节 '{section['title']}' 的 type "
                    f"'{section['type_unrecognized']}' 不是标准类型")

    extra = sorted(set(by_number) - {c["number"] for c in original})
    if extra:
        problems.append(f"展开后多出了章节编号: {extra}")
    return problems


def validate_translation(zh: list[dict], en: list[dict]) -> list[str]:
    """校验英文译本与中文版结构一致(语言不同,标题/要点不比对)。

    比对:章数、章序号顺序、每章 type、小节数、每节显式字数(要点数不比,译者合并/拆分要点是合法的)。翻译失败(模型没写、
    写了旧内容、漏章漏节)不能被"文件存在"掩盖——作者改的中文要真的反映到英文。
    """
    problems = []
    if len(en) != len(zh):
        problems.append(f"章数不一致:中文 {len(zh)} 章,英文 {len(en)} 章")
    if [c["number"] for c in en] != [c["number"] for c in zh]:
        problems.append(f"章序号/顺序不一致:{[c['number'] for c in zh]} → {[c['number'] for c in en]}")
    for z, e in zip(zh, en):
        tag = f"第 {z['number']} 章"
        if z["type"] != e["type"]:
            problems.append(f"{tag} type 不一致:{z['type']} → {e['type']}")
        if len(z["sections"]) != len(e["sections"]):
            problems.append(f"{tag} 小节数不一致:中文 {len(z['sections'])},英文 {len(e['sections'])}")
            continue
        for i, (zs, es) in enumerate(zip(z["sections"], e["sections"]), start=1):
            if (zs.get("type") or "") != (es.get("type") or ""):
                problems.append(f"{tag} 第 {i} 节 type 不一致:{zs.get('type') or '-'} → {es.get('type') or '-'}")
            if bool(zs.get("synthetic")) != bool(es.get("synthetic")):
                problems.append(f"{tag} 第 {i} 节的短章/普通小节形态不一致")
            if zs.get("words_explicit") and zs.get("target_words") != es.get("target_words"):
                problems.append(f"{tag} 第 {i} 节字数不一致:{zs.get('target_words')} → {es.get('target_words')}")
    return problems


def expanded_outline_path(outline_path=None) -> Path:
    """展开产物的路径:与 outline.md 同目录。"""
    base = Path(outline_path) if outline_path is not None else Path(OUTLINE_PATH)
    return base.parent / EXPANDED_PATH_NAME


def read_expanded(path) -> list[dict]:
    """解析展开产物。解析失败按 OutlineRouteError 上抛,由调用方决定怎么报。"""
    return parse_outline(path)


def normalize_budget(expanded_en: list[dict], expanded_zh: list[dict],
                     original: list[dict]) -> list[str]:
    """脚本侧收口:规划者写的字数可能缺、可能超预算。

    - 缺字数的小节:按本章计划把该章余下字数分给它们;
    - 总量超过预算:非作者钉死的小节按比例缩小到预算内(取 25 的倍数);
    - 英文版确定后,同位置的数字原样抄给中文版(结构已由 validate_translation 校验一致)。
    返回给作者看的说明行(没有预算时为空)。没有 paper.toml 的 pages 时什么也不做。
    """
    from .outline import (page_budget, plan_chapter_words, _section_is_fixed,
                          DEFAULT_SECTION_WORDS)
    budget = page_budget()
    if not budget:
        return []
    pages, per_page = budget
    total = pages * per_page
    notes = []
    author_titles = {(c["number"], s["title"].strip())
                     for c in original for s in c["sections"] if s.get("words_explicit")}

    def pinned(chapter, section):
        return (chapter["number"], section["title"].strip()) in author_titles \
            or chapter["type"] in ("limitations",)

    plan = plan_chapter_words(original, pages, per_page)
    # 1) 补缺
    for chapter in expanded_en:
        missing = [s for s in chapter["sections"] if not s.get("words_explicit")]
        if not missing:
            continue
        have = sum(s["target_words"] for s in chapter["sections"] if s.get("words_explicit"))
        room = max(plan.get(chapter["number"], DEFAULT_SECTION_WORDS * len(missing)) - have, 100 * len(missing))
        each = max(100, int(round(room / len(missing) / 25.0)) * 25)
        for s in missing:
            s["target_words"], s["words_explicit"] = each, True
        notes.append(f"第 {chapter['number']} 章有 {len(missing)} 个小节没标字数,已按计划补上")
    # 2) 压到预算内(短章与作者钉死的小节不动)
    def total_words():
        return sum(s["target_words"] for c in expanded_en for s in c["sections"])
    now = total_words()
    if now > total:
        movable = [(c, s) for c in expanded_en for s in c["sections"]
                   if not pinned(c, s) and c["type"] not in ("abstract", "conclusion", "discussion")]
        fixed_sum = now - sum(s["target_words"] for _, s in movable)
        base = sum(s["target_words"] for _, s in movable)
        if movable and base > 0 and total > fixed_sum:
            factor = (total - fixed_sum) / base
            for _, s in movable:
                s["target_words"] = max(100, int(factor * s["target_words"] // 25) * 25)
            notes.append(f"规划者给的字数合计 {now} 词,超出 {pages} 页预算({total} 词),已按比例压到 {total_words()} 词")
    # 3) 数字抄给中文版
    for en_c, zh_c in zip(expanded_en, expanded_zh):
        for en_s, zh_s in zip(en_c["sections"], zh_c["sections"]):
            zh_s["target_words"], zh_s["words_explicit"] = en_s["target_words"], True
    return notes


def missing_word_counts(chapters: list[dict]) -> int:
    """有多少小节没标字数 —— 提醒作者补 `(~N words)` 的依据。

    按 `words_explicit` 判断而不是比对 DEFAULT_SECTION_WORDS:作者真写了
    `(~250 words)` 时那是他的决定,不该被催着再写一遍。
    """
    return sum(1 for c in chapters for s in c["sections"]
               if not s.get("words_explicit"))
