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
(小节级 type 优先 → 章 type),都查不到才退回 DEFAULT_SECTION_WORDS=250。
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
        lines.append(f"  {chapter['number']:>2}. {chapter['title']:<28} "
                     f"type={chapter['type']:<12} {has}{flag}")
    return lines


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
        f"Read '{idea_path}' first. It states the author's contribution: the core "
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
        f"- Do NOT write word counts for subsections you create. Omit the "
        f"`(~N words)` annotation entirely — length is the author's call and "
        f"will be filled in afterwards. Where the author already wrote one, "
        f"reproduce it verbatim.\n"
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
        f"`###` subsection.\n\n"
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
        f"- You do NOT write word counts — length is the author's call (page budget, "
        f"venue limits). The framework fills a sensible per-type default where the "
        f"author left it blank; the author overrides the important ones.\n\n"
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
        f"- WHAT TO MARK when the idea document does not supply something: "
        f"[DESIGN DETAIL NEEDED] for a missing mechanism, [MISSING DATA] for a "
        f"number, [CITATION NEEDED] for an unsupported claim. Never invent the "
        f"content instead.\n\n"
        f"Do not write bullets about chapter transitions or terminology "
        f"consistency. The pipeline injects the writing contract and freezes a "
        f"notation table on its own; bullets about them are noise.\n\n"
        f"## Output\n"
        f"Write the SAME chapter structure to TWO files with write_file, in exactly this "
        f"syntax and nothing else — no preamble, no explanation, no code fences:\n\n"
        f"1. '{out_en_path}' — ENGLISH version. Chapter titles, subsection titles, and "
        f"bullets all in English. This is the initial structure the drafting agent reads.\n"
        f"2. '{out_zh_path}' — CHINESE version. Chapter titles, subsection titles, and "
        f"bullets all in Chinese (match the idea document's language). This is what the "
        f"author reviews and edits; when the author edits it, the English version is "
        f"re-translated to match.\n\n"
        f"The two files must be structurally IDENTICAL: same chapters, same order, same "
        f"`type:` values, same subsections and their boundaries. Only the language of "
        f"titles and bullets differs. Reproduce every chapter in the author's order with "
        f"the author's exact title and `type:` value in BOTH files. Keep author-written "
        f"subsections (titles, order, word counts, per-section type) verbatim in BOTH "
        f"files. Write BOTH files; report only that the files were written.\n\n"
        f"Syntax (used identically in both files, English shown here):\n\n"
        f"## 4. Method\n\n"
        f"type: method\n\n"
        f"### 4.1 <subsection title>\n"
        f"- <bullet>\n"
        f"- <bullet>\n\n"
        f"### 4.2 <subsection title>\n"
        f"- <bullet>\n"
    )


def build_translate_prompt(out_zh_path: str, out_en_path: str) -> str:
    """Manager 把作者改好的中文版 outline.zh.md 翻译成英文版 outline.md。

    只翻译语言,不动结构:章、type、小节划分、字数、小节级 type 全部逐字保留。
    这是 --init 的前置步骤——作者只维护中文,Agent 吃英文,翻译由 Manager 完成。
    """
    return (
        f"You are translating a paper outline from Chinese to English.\n\n"
        f"Read '{out_zh_path}' — the author-reviewed Chinese outline.\n"
        f"Translate it into English and write the result to '{out_en_path}'.\n\n"
        f"Hard rules:\n"
        f"- Keep the STRUCTURE byte-identical: same chapters, same order, same "
        f"`type:` values, same subsections and their boundaries.\n"
        f"- Translate chapter titles, subsection titles, and bullets to English.\n"
        f"- Keep `(~N words)` word counts and per-section `- type: X` lines VERBATIM "
        f"(do not translate `type` values or numbers).\n"
        f"- Keep the first-line HTML comment marker as-is.\n"
        f"- Write ONLY '{out_en_path}' with write_file, in exactly the outline syntax "
        f"(## chapter / type: / ### section / - bullet). No preamble or explanation.\n"
    )


def build_data_index_prompt(data_root: str, out_path: str, idea_path: str) -> str:
    """Manager 生成 data-index.md 的任务。

    data/ 里是作者跑出来的原始结果(CSV/JSON/日志/图),名字与结构千差万别,Agent 直接
    读原始文件既慢又容易漏。这份索引把原始数字按三级组织——实验名 → 实验结果项 → 具体
    数值——给 data 类章节(结果/实验/消融)导航用。Manager 只负责*忠实搬运与分组*:
    每个数字必须能在 data/ 里找到原文,绝不补、不推断、不计算。数字门禁仍直读 data/
    作为 ground truth,这份索引只是 Agent 的导航。
    """
    from .content_source import load_results_store, list_plots, render_results_summary
    store = load_results_store(data_root)
    plots = list_plots(data_root)
    summary = render_results_summary(store, plots)
    return (
        f"You are building a NAVIGATION INDEX over the author's experiment results.\n\n"
        f"Read '{idea_path}' first so you know which runs/metrics are the contribution "
        f"and how the paper will name them — the index should use the SAME experiment "
        f"names the paper will cite.\n\n"
        f"Below is a faithful dump of everything found under '{data_root}' "
        f"(numeric results, textual run metadata, and plot filenames). Organize it "
        f"into a THREE-LEVEL index and write it to '{out_path}':\n\n"
        f"Level 1 = EXPERIMENT (a run / config / ablation group)\n"
        f"Level 2 = RESULT (a metric or measured outcome under that experiment)\n"
        f"Level 3 = VALUE (the specific number, exactly as recorded, with its unit)\n\n"
        f"Hard rules:\n"
        f"- Every number in the index MUST appear verbatim in the dump below. Do not "
        f"round, average, derive, or invent any value. If a value is missing, omit it.\n"
        f"- Do NOT paraphrase the run metadata (hardware/dataset/hyperparameters) — "
        f"copy it as-is under the relevant experiment.\n"
        f"- Keep it a navigation index, not prose. One number per line is fine.\n\n"
        f"Write ONLY '{out_path}'. If data/ is empty, write a file saying so and listing "
        f"nothing.\n\n"
        f"=== RAW DUMP FROM data/ ===\n{summary}\n"
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

    extra = sorted(set(by_number) - {c["number"] for c in original})
    if extra:
        problems.append(f"展开后多出了章节编号: {extra}")
    return problems


def expanded_outline_path(outline_path=None) -> Path:
    """展开产物的路径:与 outline.md 同目录。"""
    base = Path(outline_path) if outline_path is not None else Path(OUTLINE_PATH)
    return base.parent / EXPANDED_PATH_NAME


def read_expanded(path) -> list[dict]:
    """解析展开产物。解析失败按 OutlineRouteError 上抛,由调用方决定怎么报。"""
    return parse_outline(path)


def missing_word_counts(chapters: list[dict]) -> int:
    """有多少小节没标字数 —— 提醒作者补 `(~N words)` 的依据。

    按 `words_explicit` 判断而不是比对 DEFAULT_SECTION_WORDS:作者真写了
    `(~250 words)` 时那是他的决定,不该被催着再写一遍。
    """
    return sum(1 for c in chapters for s in c["sections"]
               if not s.get("words_explicit"))
