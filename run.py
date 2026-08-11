"""
Experiment-Paper Multi-Agent Writing Framework — Main Entry Point

Content source is data/ (experiment results) when PAPER_MODE=experiment, or
references/ notes when PAPER_MODE=survey. Chapter workspaces live under workspace/.

写一整篇论文。章节文件夹全部由 outline.md 经 `--init` 生成:

  python run.py --expand               # 章节骨架 → Manager 补小节与要点 → outline.expanded.md
                                           #   审阅它,补 (~N words),改名成 outline.md
  python run.py --init                 # outline.md → workspace/<NN-type>/ + 跨章状态
  python run.py --init --force         #   outline 改过之后刷新已存在的 brief.md
  python run.py --retrieve             # 可选:按章检索文献候选 → references/candidates.md
                                           #   读 idea+outline+data,每章生成 query,
                                           #   过 verify_url 去重,候选清单(不进 bib)
  python run.py --all --progress       # 按章号顺序跑全部,失败即停
  python run.py 04-method --progress   #   也可以只跑其中一章(必须在 outline 里)

每章都是整篇里的第 N 章,知道前后是什么,符号沿用前章的定义(跨章状态文件
workspace/cross-chapter-state.md 由每章跑完后自动追加),开头接上文、结尾引下文。

  python run.py --list                 # 列出所有章节文件夹及其状态

outline.md 的分工:**你定结构**(分几章、每章什么 `type:`、什么顺序),`--expand`
让 Manager 读 idea.md 补**施工细节**(每章拆哪几节、每节写什么、边界在哪)。
产物落在 outline.expanded.md,你审完补上字数、改名成 outline.md 再 --init。
不想用 --expand 就自己把小节和要点写进 outline.md,--init 只认这一个文件。

没有小节的章会被 --init 拒绝:那样生成的 brief 会让起草退回单段、要点为空,
Draft 只能照章标题硬编,而流水线全程显示成功。

前置条件(见 TODO.md):填 idea.md、配 .env 的三组 key;data 类章节还需要 data/。
"""
import os
import sys
import warnings
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

# 压制 LLM 生成的 Python 代码里偶发的非法转义提示（如 "...\("）。
# 这是 smolagents 的 python_executor 在 compile() 模型产物时触发，源头在模型
# 生成的代码而非本框架（框架代码 compile 零 warning）。属于无害运行时噪音，
# Python 仍正常执行；过滤掉只为让终端输出干净。
warnings.filterwarnings("ignore", category=SyntaxWarning)

from config import PAPER_ROOT, PAPER_MODE, get_draft_model, get_review_model, get_manager_model
from agents import create_agents, run_4stage_via_manager, run_4stage_via_manager_stream, run_4stage_direct, run_4stage_with_progress


def list_paper_folders():
    """列出所有章节文件夹及其状态(整篇第几章)。"""
    from agents.outline import resolve_write_mode, OutlineRouteError
    print(f"Paper root: {PAPER_ROOT}\n")
    for name in sorted(os.listdir(PAPER_ROOT)):
        folder = os.path.join(PAPER_ROOT, name)
        if not os.path.isdir(folder) or name.startswith("_"):
            continue      # `template` 等脚手架目录不是章节
        has_brief = os.path.exists(os.path.join(folder, "brief.md"))
        done = os.path.exists(os.path.join(folder, "final.md"))
        status = "done" if done else "ready" if has_brief else "no brief.md"
        try:
            info = resolve_write_mode(name)
            route = f"整篇 {info['position']}/{info['total']}"
        except OutlineRouteError as exc:
            # 不在 outline 里的文件夹不是合法章节:outline 坏了或没 --init。
            route = "不在 outline(先 --init)"
        print(f"  [{status:12s}] {name:<26} {route}")


def is_chapter_folder(name: str) -> bool:
    """这个目录是不是一个可跑的章节工作区。

    `_` 前缀的是脚手架而非章节。不排掉的话它会出现在 `--list` 里,甚至被误跑。
    """
    return (not name.startswith("_")
            and os.path.isdir(os.path.join(PAPER_ROOT, name))
            and os.path.exists(os.path.join(PAPER_ROOT, name, "brief.md")))


def chapter_folders_in_order():
    """含 brief.md 的章节文件夹,按名字排序(`--init` 生成的 NN- 前缀即运行顺序)。"""
    return [name for name in sorted(os.listdir(PAPER_ROOT))
            if is_chapter_folder(name)]


def outline_chapters_in_order():
    """`--all` 的运行清单:workspace 必须与 outline.md 完全一致,按 outline 顺序。

    缺 outline 里的章 = 还没 `--init`;多出磁盘目录 = 改标题后改名遗留。两种都拒绝
    开始,避免 `--all` 只跑一个子集却报告"全部完成"。outline 解析失败同样抛错。
    """
    from agents.outline import parse_outline, OutlineRouteError
    outline_folders = [c["folder"] for c in parse_outline()]
    on_disk = set(chapter_folders_in_order())
    missing = [name for name in outline_folders if name not in on_disk]
    extra = sorted(on_disk - set(outline_folders))
    if missing or extra:
        details = []
        if missing:
            details.append("缺少目录: " + ", ".join(missing))
        if extra:
            details.append("不在 outline 的目录: " + ", ".join(extra))
        raise OutlineRouteError(
            "workspace 与 outline.md 不一致(" + "; ".join(details) + ")。"
            "先跑 `python run.py --init`;遗留目录把 input.md 迁出后清理。"
        )
    return outline_folders, []


def _maybe_build_data_index() -> None:
    """--init 末尾:若 data/data-index.md 不存在,让 Manager 读 data/ 生成它。

    存在则跳过(作者可能审改过,不覆盖)。data/ 为空(没有真实结果)时也跳过:
    data-index 是 data 类章节写正文的数字导航,没数字可索引时硬造一份空索引只会误导。
    等作者补了 data/ 结果,删掉它重跑 --init 即可重建。
    """
    from config import DATA_INDEX_PATH, DATA_ROOT
    if DATA_INDEX_PATH.exists():
        print(f"\ndata-index: {DATA_INDEX_PATH.name} 已存在,跳过(不覆盖)。"
              f"要重建就删掉它再 --init。")
        return
    try:
        from agents.content_source import data_dir_has_content
        if not data_dir_has_content(str(DATA_ROOT)):
            print(f"\ndata-index: data/ 是空目录,跳过 data-index 生成。")
            print(f"补好实验结果后,删掉 {DATA_INDEX_PATH} 再跑 python run.py --init 重建。")
            return
    except Exception as exc:
        print(f"\ndata-index: 扫描 data/ 失败,跳过: {str(exc)[:120]}")
        return

    from config import get_manager_model, IDEA_PATH
    from agents import create_planner_agent
    from agents.outline_expand import build_data_index_prompt
    from agents.orchestrator import run_agent_stage_standalone

    print(f"\ndata-index: 让 Manager 读 data/ 生成 {DATA_INDEX_PATH.name} "
          f"(三级索引:实验 → 结果 → 数值)...")
    DATA_INDEX_PATH.parent.mkdir(parents=True, exist_ok=True)
    manager = create_planner_agent(get_manager_model())
    prompt = build_data_index_prompt(str(DATA_ROOT), str(DATA_INDEX_PATH), str(IDEA_PATH))
    run_agent_stage_standalone(manager, "Manager", prompt)
    if DATA_INDEX_PATH.is_file():
        print(f"data-index: 已生成 {DATA_INDEX_PATH.name}。data 类章节写正文时读它导航。")
    else:
        print(f"data-index: Manager 没写出 {DATA_INDEX_PATH.name};"
              f"data 类章节将缺少导航,可重跑 --init 重试。")


def _translate_zh_to_en() -> int:
    """若作者改过 outline.zh.md(比 outline.md 新),用 Manager 把中文翻译成英文覆盖。

    作者只维护中文版 outline.zh.md;英文版 outline.md 是 Agent 吃的结构。--init 前
    检查:中文版更新则重新翻译覆盖英文版,保证 Agent 读到的英文始终反映作者最新修改。
    返回 0=不需要翻译/成功,1=翻译失败。
    """
    from agents.outline import OUTLINE_PATH, OUTLINE_DRAFT_PATH
    from agents.outline_expand import ZH_EXPANDED_PATH_NAME, build_translate_prompt
    zh_path = Path(OUTLINE_PATH.parent) / ZH_EXPANDED_PATH_NAME
    en_path = OUTLINE_PATH
    if not zh_path.is_file():
        return 0   # 没有中文版(--expand 没跑过),无需翻译
    # 中文版比英文版新,或英文版不存在 → 需要重新翻译覆盖
    if en_path.is_file() and en_path.stat().st_mtime >= zh_path.stat().st_mtime:
        return 0   # 英文版不比中文旧,无需覆盖
    print(f"\n检测到 {zh_path.name}(中文版)比 {en_path.name}(英文版)新,"
          f"重新翻译英文版以反映你的修改...")
    from config import get_manager_model
    from agents import create_planner_agent
    from agents.orchestrator import run_agent_stage_standalone
    manager = create_planner_agent(get_manager_model())
    prompt = build_translate_prompt(str(zh_path), str(en_path))
    run_agent_stage_standalone(manager, "Manager", prompt)
    if not en_path.is_file():
        print(f"Error: Manager 没有写出英文版 {en_path.name}。")
        return 1
    print(f"翻译完成:{en_path.name} 已由 {zh_path.name} 覆盖。")
    return 0


def init_workspaces(force: bool) -> int:
    """读 outline.md 生成各章工作区 + 跨章状态。返回退出码。

    这条路会创建 cross-chapter-state.md——跨章术语与结论的载体,每章跑完由 Stage 5
    追加。
    """
    from agents.outline import (chapters_without_sections,
                                init_chapter_workspaces, outline_banner,
                                parse_outline, OUTLINE_PATH, OutlineRouteError,
                                CrossChapterStateError)
    # 作者只改中文版 outline.zh.md;若它比 outline.md 新,--init 先翻译覆盖英文版,
    # 再读英文版生成工作区——保证 Agent 读到的英文反映作者最新修改。
    if _translate_zh_to_en() != 0:
        return 1
    if not Path(OUTLINE_PATH).is_file():
        print(f"Error: 找不到总纲 {OUTLINE_PATH}")
        print("先复制模板:  cp outline.example.md outline.md")
        print("填好章节结构后再跑 python run.py --init")
        return 1

    # 只填了表格骨架就 --init,是最容易踩的一步:章标题与 type 都对,但没有任何
    # 小节与起草要点。那样生成的 brief 会让起草退回单段、要点为空,Draft 只能照
    # 章标题硬编,而流水线全程显示成功。必须在建工作区之前拦住。
    try:
        skeleton = parse_outline()
    except OutlineRouteError as exc:
        print(f"Error: {exc}")
        return 1
    bare = chapters_without_sections(skeleton) if skeleton else []
    if bare:
        from agents.outline import SHORT_CHAPTER_TYPES
        print(f"Error: {OUTLINE_PATH} 里有 {len(bare)} 章没有小节,未生成工作区:")
        for chapter in bare:
            print(f"  {chapter['number']:>2}. {chapter['title']}  "
                  f"(type: {chapter['type']})")
        print(f"\n这些章只有标题和 type,没有任何起草要点。照这样 --init,")
        print(f"起草会退回单段、拿不到任何要点,写出来的只是照章标题硬编的内容。")
        short = ", ".join(sorted(SHORT_CHAPTER_TYPES))
        print(f"\n补要点的路:")
        print(f"  · 短章({short})直接把要点写在 `## 章标题` 下:")
        print(f"      ## 1. Abstract")
        print(f"      type: abstract")
        print(f"      - 一句问题、一句方法、一句结果")
        print(f"  · 其它章写 `### N.M 小节标题` + `- 要点`(每章 2-4 个小节)")
        print(f"  · 或跑 python run.py --expand,让 Manager 读 idea.md 自动补(推荐)")
        return 1

    try:
        result = init_chapter_workspaces(force=force, chapters=skeleton)
    except (OutlineRouteError, CrossChapterStateError, OSError) as exc:
        print(f"Error: --init 未修改工作区: {exc}")
        return 1
    if not result["chapters"]:
        print(f"Error: {OUTLINE_PATH} 里没解析出任何章节。")
        print("每章需要一个 `## N. 标题` 标题行,下面跟一行 `type: <类型>`。")
        return 1

    print(f"Outline: {OUTLINE_PATH}")
    print(f"Workspace: {PAPER_ROOT}\n")
    for line in outline_banner(result):
        print(line)

    counts = (f"\n新建 {len(result['created'])} 章"
              f",跳过 {len(result['skipped'])} 章(已存在)")
    if result["updated"]:
        counts += f",刷新 {len(result['updated'])} 章"
    if result["stale"]:
        counts += f",{len(result['stale'])} 章已过期"
    print(counts)

    if result["stale"]:
        print("\n以下章节的 brief.md 与当前 outline.md 不一致,或不是 --init 生成的:")
        for name in result["stale"]:
            print(f"  {name}")
        print("确认要用 outline 的新结构覆盖它们,就跑: python run.py --init --force")

    # --init 末尾:若 data/data-index.md 不存在,让 Manager 读 data/ 原始结果生成它。
    # 它是 data 类章节(结果/实验/消融)写正文时的数字导航;数字门禁仍直读 data/ 作
    # ground truth,这份索引只供 Agent 导航。存在则跳过——作者可能审改过,不覆盖。
    _maybe_build_data_index()

    on_disk = set(chapter_folders_in_order())
    ready = [chapter["folder"] for chapter in skeleton
             if chapter["folder"] in on_disk]
    if ready:
        print(f"\n下一步:跑第一章(它会先生成本章 input.md,再挖证据、起草):")
        print(f"  python run.py \"{ready[0]}\" --progress     # 跑第一章")
        print(f"  python run.py --all --progress            # 按 outline 顺序跑全部")
        print(f"\n这些章都走【整篇模式】:知道自己是第几章,符号沿用前章,"
              f"跨章约定写在 {PAPER_ROOT}/cross-chapter-state.md。")
    return 0


def run_retrieve_cli() -> int:
    """`--retrieve`:可选前置步骤,按章检索文献候选 → references/candidates.md(不进 bib)。

    核心逻辑在 agents.retrieve.run_retrieve。这里只做终端 banner / 计数 / 下一步提示,
    风格对齐 init_workspaces / _maybe_build_data_index。
    """
    from agents.retrieve import run_retrieve
    from config import RETRIEVAL_MODEL, RETRIEVAL_API_BASE, IDEA_PATH, DATA_ROOT

    print(f"\n{'='*60}")
    print(f"文献检索(可选前置):读 idea + outline + data,按章生成检索候选")
    print(f"{'='*60}")
    print(f"  · Idea:      {IDEA_PATH}")
    print(f"  · Outline:   (见 workspace 下已 init 的章节)")
    print(f"  · Data:      {DATA_ROOT}")
    print(f"  · 检索模型:  {RETRIEVAL_MODEL or '(未配置)'} @ {RETRIEVAL_API_BASE or '(未配置)'}")
    print(f"  · 产物:      references/candidates.md(CAN- 前缀,不进 bib)")

    code, stats = run_retrieve()
    if code != 0:
        return code

    queries_path = stats.get("queries_path", "")
    candidates_path = stats.get("candidates_path", "")
    print(f"\n检索完成:")
    print(f"  · {stats.get('chapters', 0)} 章 / {stats.get('queries', 0)} 个 query")
    print(f"  · 原始命中 {stats.get('raw_hits', 0)} → 去重后 "
          f"{stats.get('candidates', 0)} 条候选"
          f"(无效/不可达 {stats.get('filtered', 0)}，重复 {stats.get('duplicates', 0)})")
    print(f"  · per-章 query: {Path(queries_path).name}")
    print(f"  · 候选清单:    {candidates_path}")
    print(f"\n下一步:人工扫一眼 {Path(candidates_path).name},挑中的行复制到 "
          f"references/bibliography.md(ID 改 REF-xxxx、补 Grade 与用途、删 Chapter/Query 列),")
    print(f"再跑 python run.py --all --progress。候选不会自动进引用池；题名、作者与内容仍需人工核对。")
    return 0


def expand_outline() -> int:
    """`--expand`:让 Manager 把章节骨架展开成带小节与要点的 outline。返回退出码。

    分工的分界线在这里:作者定**结构**(分几章、什么 type、什么顺序),Manager 补
    **施工细节**(每章拆哪几节、每节写什么、边界在哪)。后者是从 idea.md 推导出来
    的,正是规划该干的活;前者是作者对自己工作的判断,Manager 手里没有投稿目标和
    篇幅预算,不该替他决定。

    输入是作者填的草稿 `outline_draft.md`(表格骨架),输出是中英双轨两份:
    - `outline.md`(英文版)是喂给 Agent 的结构,--init 的输入。
    - `outline.zh.md`(中文版)给作者看/改,结构与英文版逐字一致。
    """
    from agents.outline import (OUTLINE_DRAFT_PATH, OUTLINE_PATH,
                                OutlineRouteError, parse_outline)
    from agents.outline_expand import (
        EN_EXPANDED_MARKER, ZH_EXPANDED_MARKER,
        EN_EXPANDED_PATH_NAME, ZH_EXPANDED_PATH_NAME,
        build_expand_prompt, missing_word_counts, read_expanded,
        render_outline, skeleton_report, validate_expansion,
    )
    from config import IDEA_PATH

    # 输入是用户填的草稿(outline_draft.md),不是定稿(outline.md)。
    if not Path(OUTLINE_DRAFT_PATH).is_file():
        print(f"Error: 找不到 {OUTLINE_DRAFT_PATH}")
        print("先写一份章节骨架(每章两行:`## N. 标题` + `type: <类型>`)。")
        print("类型词表见 outline.example.md。")
        return 1

    try:
        chapters = parse_outline(OUTLINE_DRAFT_PATH)
    except OutlineRouteError as exc:
        print(f"Error: {exc}")
        return 1
    if not chapters:
        print(f"Error: {OUTLINE_DRAFT_PATH} 里没解析出任何章节。")
        print("每章需要一个 `## N. 标题` 标题行,下面跟一行 `type: <类型>`。")
        return 1

    # idea.md 是展开的唯一依据。没有它,Manager 只能照章节标题编小节——那种
    # 结构对任何一篇论文都成立,也就对这一篇毫无价值,不如不跑。
    if not Path(IDEA_PATH).is_file():
        print(f"Error: 找不到 {IDEA_PATH}")
        print("展开小节要从你的创新点推导。先 cp idea.example.md idea.md 并填写")
        print("第 3 节(核心洞察)与第 4 节(方法设计),再跑 --expand。")
        return 1

    en_path = Path(OUTLINE_PATH.parent) / EN_EXPANDED_PATH_NAME
    zh_path = Path(OUTLINE_PATH.parent) / ZH_EXPANDED_PATH_NAME
    # 旧产物必须先清掉。留着的话本次 Manager 写失败,下面"文件存在即成功"的判断
    # 会读到上一轮的内容并原样通过——作者拿着一份陈旧展开去 --init,而终端显示成功。
    for p in (en_path, zh_path):
        if p.exists():
            print(f"Error: {p.name} 已存在(上一轮 --expand 的产物)。")
            print(f"审阅它并改名成 {Path(OUTLINE_PATH).name},或者删掉它再重跑 --expand。")
            print(f"不自动覆盖:那份文件可能已经被你改过。")
            return 1

    print(f"Outline: {OUTLINE_PATH}")
    print(f"Idea:    {IDEA_PATH}")
    print(f"\n你的章节骨架({len(chapters)} 章),Manager 不会改动它:")
    for line in skeleton_report(chapters):
        print(line)
    print(f"\nManager 将为每章补:小节拆分 + 每节的起草要点(含边界约束)。")
    print(f"字数不由它定 —— 展开后由你在小节标题后补 `(~N words)`。")
    print(f"产物中英双轨:英文版 {en_path.name}(喂 Agent)+ 中文版 {zh_path.name}(给你审阅)。")
    print(f"不动你的 {Path(OUTLINE_PATH).name}。\n")

    from config import get_manager_model
    from agents import create_planner_agent
    from agents.orchestrator import run_agent_stage_standalone

    print("Initializing manager model...")
    manager = create_planner_agent(get_manager_model())

    prompt = build_expand_prompt(chapters, str(en_path), str(zh_path),
                                 str(IDEA_PATH), len(chapters))
    run_agent_stage_standalone(manager, "Manager", prompt)

    missing_files = [p.name for p in (en_path, zh_path) if not p.is_file()]
    if missing_files:
        print(f"\nError: Manager 没有写出 {', '.join(missing_files)}。")
        print("重跑 --expand;或者自己往 outline.md 里补小节。")
        return 1

    # 英文版是喂给 Agent 的结构,以它为准做结构校验。中文版是给作者的对照,
    # 结构与英文版必须一致——Manager 被要求逐字一致,这里也校验一遍。
    try:
        expanded_en = read_expanded(en_path)
        expanded_zh = read_expanded(zh_path)
    except OutlineRouteError as exc:
        print(f"\nError: 展开产物解析不出章节: {exc}")
        print(f"文件保留在原地,你可以自己修,或删掉它重跑 --expand。")
        return 1

    problems = validate_expansion(chapters, expanded_en)
    if problems:
        # 越权改结构必须拦。Manager 悄悄合并两章或改掉一个 type,--init 照样会
        # 生成工作区,作者要到看见文件夹名不对才发现——那时 token 已经花了。
        print(f"\nError: {en_path.name} 改动了你的章节结构,拒绝采纳:")
        for problem in problems:
            print(f"  · {problem}")
        print(f"\n文件保留在 {en_path.name} 供你查看。要么手改它,"
              f"要么删掉重跑 --expand。")
        return 1

    # 中文版的结构校验:章数、顺序、type 必须与英文版一致(只比对结构,不比对
    # 语言内容)。Manager 被要求逐字一致,这里做兜底——若中文版漏章/改了 type,
    # 作者改它时会把英文版的结构也带偏。
    zh_problems = validate_expansion(chapters, expanded_zh)
    if zh_problems:
        print(f"\nError: {zh_path.name} 结构与骨架不一致,拒绝采纳:")
        for problem in zh_problems:
            print(f"  · {problem}")
        print(f"\n文件保留在 {zh_path.name} 供你查看。要么手改它,"
              f"要么删掉重跑 --expand。")
        return 1

    # 重新序列化一遍再落盘:Manager 的原始输出可能夹带说明文字或代码围栏,
    # 而这份文件下一步要被作者改名成 outline.md,必须是干净的语法。
    title = f"Paper outline — {Path(OUTLINE_PATH).stem}"
    en_path.write_text(render_outline(expanded_en, title, EN_EXPANDED_MARKER),
                       encoding="utf-8")
    zh_title = f"论文大纲 — {Path(OUTLINE_PATH).stem}"
    zh_path.write_text(render_outline(expanded_zh, zh_title, ZH_EXPANDED_MARKER),
                       encoding="utf-8")

    total_sections = sum(len(c["sections"]) for c in expanded_en)
    print(f"\n展开完成:{len(expanded_en)} 章,{total_sections} 个小节 → "
          f"{en_path.name} + {zh_path.name}")
    for chapter in expanded_en:
        titles = " / ".join(s["title"] for s in chapter["sections"])
        print(f"  {chapter['number']:>2}. {chapter['title']}: {titles}")

    pending = missing_word_counts(expanded_en)
    if pending:
        # 区分普通章（小节级字数，写在 ### 标题）与短章（章级字数，写在 ## 标题）：
        # 短章的合成小节 words_explicit=False 时也算"待标"，但它的字数该写在 ## 行，不该新增 ###。
        from agents.outline import SHORT_CHAPTER_TYPES
        short_pending = sum(
            1 for c in expanded_en for s in c["sections"]
            if s.get("synthetic") and not s.get("words_explicit"))
        regular_pending = pending - short_pending
        print(f"\n{pending} 个起草单元还没有字数标注,当前按各 type 的经验默认值处理。")
        if regular_pending:
            print(f"  · 普通章在小节标题后加字数,例如 `### 4.1 总体框架 (~350 words)`。")
        if short_pending:
            short_list = ", ".join(sorted(SHORT_CHAPTER_TYPES))
            print(f"  · 短章({short_list})在**章标题**后加字数,"
                  f"例如 `## 1. Abstract (~200 words)`;不要新增 `###` 小节。")
    print(f"\n下一步:")
    print(f"  1. 读 {zh_path.name}(中文版),改掉不认同的小节与要点,补上字数")
    print(f"  2. python run.py --init   # 检测到中文版更新,自动翻译成英文 outline.md 再生成工作区")
    return 0


def chapter_recorded_in_cross_state(name: str) -> bool:
    """本章有没有在跨章状态的 Key Claims 里留下条目——Stage 5 的完成标志。

    只在 Key Claims 小节里找,不能全文搜:文件顶部有一份章节顺序清单,里面本来
    就写着每个文件夹名,全文搜会让每一章都显示"已完成跨章交接"。
    """
    from agents.outline import CROSS_CHAPTER_STATE
    from agents.orchestrator import cross_chapter_state_has_claim
    path = Path(PAPER_ROOT) / CROSS_CHAPTER_STATE
    if not path.is_file():
        return False
    return cross_chapter_state_has_claim(
        path.read_text(encoding="utf-8", errors="replace"), name)


def chapter_is_complete(name: str) -> bool:
    """一章算不算跑完:有 final.md **且**跨章状态已交接。

    只看 final.md 是不够的:Stage 5 失败时 final.md 已经落盘,重跑 --all 会直接
    跳过这一章,于是它的术语约定永远进不了跨章状态,后面每一章都各写一套。
    """
    return (os.path.exists(os.path.join(PAPER_ROOT, name, "final.md"))
            and chapter_recorded_in_cross_state(name))


def _print_paper_table(folders: list[str], rows: list[tuple]) -> None:
    """打印整篇章节汇总表。rows 是 (position, name, status) 列表。

    `--all` 结束时(或中途失败)展示各章状态,让用户一眼看清哪些章完成/失败。
    """
    if not rows:
        return
    width_pos = max(len(str(r[0])) for r in rows)
    width_name = max(len(r[1]) for r in rows)
    width_status = max(len(r[2]) for r in rows)
    lines = [
        f"\n=== 整篇章节进度 ===",
        f"| {'#':<{width_pos}} | {'章节':<{width_name}} | {'状态':<{width_status}} |",
        f"|{'-' * (width_pos + 2)}|{'-' * (width_name + 2)}|{'-' * (width_status + 2)}|",
    ]
    for position, name, status in rows:
        lines.append(f"| {position:<{width_pos}} | {name:<{width_name}} | "
                     f"{status:<{width_status}} |")
    lines.append("======================")
    print("\n".join(lines), flush=True)


def run_all_chapters(draft_agent, review_agent, manager_agent) -> int:
    """整篇模式:按 outline.md 的顺序跑完全部章节。某章失败就停。

    继续跑只会在错误的前提上烧 token,而且前章的跨章状态没写成,后面几章会在缺失
    术语约定的前提下各自另立一套。
    """
    from agents.outline import OutlineRouteError
    try:
        folders, _ = outline_chapters_in_order()
    except OutlineRouteError as exc:
        print(f"Error: 无法从 outline.md 得出章节顺序: {exc}")
        return 1
    if not folders:
        print(f"Error: outline.md 里的章节在 {PAPER_ROOT} 下一个都不存在。")
        print("先跑 python run.py --init 从 outline.md 生成章节文件夹。")
        return 1

    print(f"章节顺序来自 outline.md({len(folders)} 章):")
    for position, name in enumerate(folders, start=1):
        done = chapter_is_complete(name)
        print(f"  {'[done] ' if done else '       '}{position}/{len(folders)}  {name}")

    completed, skipped = [], []
    chapter_rows = []   # (position, name, status) 用于整篇章节汇总表
    for position, name in enumerate(folders, start=1):
        folder_path = os.path.join(PAPER_ROOT, name)
        if chapter_is_complete(name):
            print(f"\n{'=' * 60}\n[{position}/{len(folders)}] {name} — 已完成,跳过\n{'=' * 60}")
            skipped.append(name)
            chapter_rows.append((position, name, "[完成] 跳过"))
            continue

        print(f"\n{'=' * 60}\n[{position}/{len(folders)}] {name}\n{'=' * 60}")
        result = run_4stage_with_progress(
            draft_agent, review_agent, folder_path, manager_agent)

        if not os.path.exists(os.path.join(folder_path, "final.md")):
            # pre-flight 拒绝(缺 idea.md / 缺 data/)或某阶段失败,都会走到这里。
            # 后面的章节大概率同因失败,而且前章的 cross-chapter-state 没写成,
            # 继续跑等于让后续章节在缺失上下文的前提下生成。
            chapter_rows.append((position, name, "[失败] 未产出 final.md"))
            print(f"\n[run-all] 停止:{name} 未产出 final.md。")
            print(f"[run-all] 上方日志有原因(常见:idea.md 未填、data/ 为空、模型调用失败)。")
            print(f"[run-all] 已完成 {len(completed)} 章,修好后重跑 --all 会从这一章继续。")
            _print_paper_table(folders, chapter_rows)
            return 1
        if result.get("stage5_xchap_ok") is not True:
            # final.md 有了,但跨章交接没做成。继续跑下一章 = 让它在没有本章术语
            # 约定的前提下另起一套,而 final.md 的存在会让重跑永远跳过这一章。
            chapter_rows.append((position, name, "[失败] 跨章交接未完成"))
            print(f"\n[run-all] 停止:{name} 的 final.md 已生成,但跨章状态没交接成功。")
            print(f"[run-all] 下一章会拿不到本章的术语约定,术语从这里开始漂。")
            print(f"[run-all] 检查 {PAPER_ROOT}/cross-chapter-state.md 后重跑 --all,")
            print(f"[run-all] 本章只会重跑 Stage 5(前面的产物都在)。")
            _print_paper_table(folders, chapter_rows)
            return 1
        completed.append(name)
        chapter_rows.append((position, name, "[完成]"))

    print(f"\n{'=' * 60}")
    print(f"[run-all] 全部完成:本次生成 {len(completed)} 章,跳过 {len(skipped)} 章。")
    print(f"[run-all] 各章产物在 {PAPER_ROOT}/<章节>/final.md 与 final.zh.md")
    print(f"[run-all] 编译整篇: python latex/build.py")
    _print_paper_table(folders, chapter_rows)
    return 0


def main():
    # --retrieve 是独立命令,禁止与章名、--all、--init、--expand 等组合,避免参数被静默吞掉。
    if "--retrieve" in sys.argv[1:] and sys.argv[1:] != ["--retrieve"]:
        print("Error: --retrieve 是独立命令,不能与章名、--all、--init 或其它参数组合。")
        print("Run: python run.py --retrieve")
        sys.exit(2)

    if "--list" in sys.argv:
        list_paper_folders()
        return

    # --init 不调模型,单独处理并提前返回。
    if "--init" in sys.argv:
        sys.exit(init_workspaces(force="--force" in sys.argv))

    # --expand 只调 Manager 一次,不进章节流水线,同样提前返回。
    if "--expand" in sys.argv:
        sys.exit(expand_outline())

    # --retrieve: 可选前置步骤。按章检索文献候选 → references/candidates.md(不进 bib)。
    # 读 idea+outline+data,Manager 生成 per-章 query,Python 执行检索+验 URL+去重。
    if "--retrieve" in sys.argv:
        sys.exit(run_retrieve_cli())

    run_all = "--all" in sys.argv

    if not run_all and (len(sys.argv) < 2 or sys.argv[1].startswith("--")):
        print(__doc__)
        print("Available folders:")
        list_paper_folders()
        return

    folder_name = None if run_all else sys.argv[1]
    use_direct = "--direct" in sys.argv
    use_stream_raw = "--stream-raw" in sys.argv
    use_progress = "--progress" in sys.argv

    if sum([use_direct, use_stream_raw, use_progress]) > 1:
        print("Error: --direct, --progress, --stream-raw are mutually exclusive.")
        sys.exit(1)

    if run_all and (use_direct or use_stream_raw):
        print("Error: --all 只支持 --progress(整篇流水线需要完整的门禁与收敛循环)。")
        sys.exit(1)

    # C1: in experiment mode, only --progress runs the full mode-aware pipeline
    # (evidence mining, content source, number gate, convergence loop). The
    # Manager/direct/stream-raw paths are survey-only and would read the wrong
    # files and bypass every experiment safeguard — refuse them explicitly.
    if PAPER_MODE == "experiment" and not use_progress:
        print("Error: experiment mode requires --progress.")
        print("The --direct / --stream-raw / default-Manager paths are survey-only and")
        print("would skip the content source, number gate, and convergence loop.")
        print("Run: python run.py \"<workspace>\" --progress")
        sys.exit(1)

    folder_path = None
    if not run_all:
        folder_path = os.path.join(PAPER_ROOT, folder_name)

        # `_` 前缀是脚手架而非章节。--list 与 --all 已经排掉它们,但显式点名仍会跑。
        if folder_name.startswith("_"):
            print(f"Error: '{folder_name}' 是脚手架目录(`_` 前缀),不是可跑的章节。")
            print("章节文件夹由 outline.md 经 --init 生成:  python run.py --init")
            sys.exit(1)

        if not os.path.isdir(folder_path):
            print(f"Error: folder '{folder_path}' does not exist.")
            print("现有章节:")
            list_paper_folders()
            print("\n从 outline.md 生成章节文件夹: python run.py --init")
            sys.exit(1)

        # 先确认这一章在 outline 里。不在 outline 的文件夹不是合法章节——即使它
        # 也缺 brief.md,首要错误仍是路由非法,不该报成"缺 brief"。
        from agents.outline import resolve_write_mode, OutlineRouteError
        try:
            info = resolve_write_mode(folder_name)
        except OutlineRouteError as exc:
            print(f"\nError: '{folder_name}' 不是合法章节: {exc}")
            sys.exit(1)

        brief_path = os.path.join(folder_path, "brief.md")
        if not os.path.exists(brief_path):
            print(f"Error: {brief_path} not found. Create brief.md first.")
            print("从 outline.md 生成: python run.py --init")
            sys.exit(1)

        print(f"\n路由:outline.md 的第 {info['position']}/{info['total']} 章")
        print(f"  · 可跨章引用,符号沿用前章(见 {PAPER_ROOT}/cross-chapter-state.md)")
        print(f"  · 跑完会把本章的术语与结论追加进跨章状态,供后续章节对齐")

    print(f"Initializing models...")
    model_draft = get_draft_model()
    model_review = get_review_model()
    model_manager = get_manager_model()

    print(f"Creating agents...")
    manager, draft_agent, review_agent = create_agents(model_draft, model_review, model_manager)

    if run_all:
        print(f"\n{'='*60}")
        print(f"整篇模式:按 outline.md 的顺序跑完 {PAPER_ROOT} 下的各章")
        print(f"{'='*60}")
        sys.exit(run_all_chapters(draft_agent, review_agent, manager))

    mode = "direct" if use_direct else "progress" if use_progress else "stream-raw" if use_stream_raw else "manager"
    print(f"\n{'='*60}")
    print(f"Running 4-stage iteration on: {folder_name}")
    print(f"Mode: {mode}")
    print(f"{'='*60}")

    if use_progress:
        results = run_4stage_with_progress(draft_agent, review_agent, folder_path, manager)
        print(f"\nStages completed: {len(results)}")
    elif use_direct:
        results = run_4stage_direct(draft_agent, review_agent, folder_path)
        print(f"\nStages completed: {len(results)}")
    elif use_stream_raw:
        events = run_4stage_via_manager_stream(manager, folder_path, raw=True)
        print(f"\nStream events received: {len(events)}")
    else:
        result = run_4stage_via_manager(manager, folder_path)
        print(f"\nManager result:\n{str(result)[:500]}")


if __name__ == "__main__":
    main()
