"""
Experiment-Paper Multi-Agent Writing Framework — Main Entry Point

一篇论文 = 一个项目文件夹;材料全部放进它的 inbox/:

  python run.py --new 我的论文                       # 在 projects/ 下建项目 + 模板
  # 把 idea / 结果表 / 文献 / 代码放进 projects/我的论文/inbox/(见 inbox/说明.md)
  python run.py --project projects/我的论文 --go     # 材料检查 → 建索引 → 展开大纲 → 停下等你审
  # 审 projects/我的论文/outline.zh.md(改小节、要点、字数)
  python run.py --project projects/我的论文 --go     # 逐章生成 → 整篇组装到 output/

出错 / 中断 / 手改终稿后,重跑同一条命令即可:已完成的章自动跳过,手改过的终稿只重新核验
和组装,不重写。

分步命令(都要带 --project):
  --check      只做材料检查(output/材料检查.md)
  --index      只建三份索引(文献 / 数据 / 代码)
  --expand     章节骨架 → 规划者读 idea.md 补小节与要点 → outline.zh.md + _work/outline.md
  --init       outline → 各章工作区 + 跨章状态(--force 刷新已存在的 brief.md)
  --all        按章号顺序跑全部章节,失败即停
  <章节名>     只跑其中一章(必须在 outline 里)
  --assemble   重新核验各章终稿并组装 output/(不调模型)
  --list       列出各章状态
  --keep-outline  idea.md / 骨架改过后,确认继续沿用旧大纲(配合 --go)

每章都是整篇里的第 N 章,知道前后是什么,符号沿用前章的定义(跨章状态文件由每章跑完后
自动追加),开头接上文、结尾引下文。
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


def _early_project_env() -> None:
    """`--project <路径>` 必须在 import config 之前放进环境变量:config 的所有路径都从
    它派生,并且在 import 时就冻结。"""
    argv = sys.argv[1:]
    if "--project" in argv:
        i = argv.index("--project")
        if i + 1 < len(argv):
            os.environ["PAPER_PROJECT"] = str(Path(argv[i + 1]).expanduser())


_early_project_env()

from config import PAPER_ROOT, get_draft_model, get_review_model, get_manager_model
from agents import create_agents, run_4stage_with_progress


def list_paper_folders():
    """列出所有章节文件夹及其状态(整篇第几章)。"""
    from agents.outline import resolve_write_mode, OutlineRouteError
    print(f"Paper root: {PAPER_ROOT}\n")
    if not os.path.isdir(PAPER_ROOT):
        print("  (还没有章节工作区;先 --go 或 --init)")
        return
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
    if not os.path.isdir(PAPER_ROOT):
        return []
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


def _translate_zh_to_en() -> int:
    """若作者改过 outline.zh.md(内容指纹与上次同步时不同),用 Manager 把中文翻译成英文覆盖。

    作者只维护中文版 outline.zh.md;英文版 _work/outline.md 是 Agent 吃的结构。翻译写进
    候选文件,结构校验(章数/顺序/type/小节数/显式字数)通过才原子替换:翻译失败时旧英文版
    原样保留、命令报错,不会"文件存在"就当成功。同步状态用内容指纹而不是 mtime。
    返回 0=不需要翻译/成功,1=翻译失败。
    """
    from agents import project
    from agents.outline import OUTLINE_PATH, OUTLINE_ZH_PATH, OutlineRouteError, parse_outline
    from agents.outline_expand import build_translate_prompt, validate_translation
    zh_path, en_path = Path(OUTLINE_ZH_PATH), Path(OUTLINE_PATH)
    if not zh_path.is_file():
        return 0   # 没有中文版(--expand 没跑过),无需翻译
    zh_sha = project.sha_file(zh_path)
    state = project.load_state()
    if en_path.is_file() and state.get("zh") == zh_sha:
        return 0   # 英文版已反映当前中文版
    print(f"\n检测到 {zh_path.name}(中文版)有改动,重新翻译英文版以反映你的修改...")
    from config import get_manager_model
    from agents import create_planner_agent
    from agents.orchestrator import run_agent_stage_standalone
    en_path.parent.mkdir(parents=True, exist_ok=True)
    manager = create_planner_agent(get_manager_model())
    candidate = en_path.with_name(en_path.name + ".candidate")
    if candidate.exists():
        candidate.unlink()
    prompt = build_translate_prompt(str(zh_path), str(candidate),
                                    str(en_path) if en_path.is_file() else "")
    run_agent_stage_standalone(manager, "Manager", prompt, allowed_files=[candidate])
    if not candidate.is_file():
        print(f"Error: Manager 没有写出翻译候选文件;{en_path.name} 未改动。")
        return 1
    try:
        problems = validate_translation(parse_outline(zh_path), parse_outline(candidate))
    except OutlineRouteError as exc:
        problems = [f"译本解析失败: {exc}"]
    if not problems and en_path.is_file():
        # 章文件夹名来自英文标题:译本把已有章的标题改了措辞,工作区就会对不上(已写好的章成孤儿)
        try:
            old_f = [c["folder"] for c in parse_outline(en_path)]
            new_f = [c["folder"] for c in parse_outline(candidate)]
            if old_f != new_f and any((Path(PAPER_ROOT) / f).is_dir() for f in old_f):
                problems.append(f"章文件夹名会变:{old_f} → {new_f}(已有章的工作区会对不上);"
                                f"请在中文大纲里保持已生成章的标题不变,或重新展开")
        except OutlineRouteError as exc:
            problems.append(f"译本解析失败: {exc}")
    if problems:
        print(f"Error: 英文译本与 {zh_path.name} 结构不一致,拒绝采纳({en_path.name} 未改动):")
        for problem in problems:
            print(f"  · {problem}")
        print(f"候选文件留在 {candidate} 供查看;重跑会重新翻译。")
        return 1
    os.replace(candidate, en_path)
    state["zh"] = zh_sha
    project.save_state(state)
    print(f"翻译完成:{en_path.name} 已反映 {zh_path.name}。")
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
        print("先展开大纲:  python run.py --project <项目> --expand")
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
        print("确认要用 outline 的新结构覆盖它们,就跑: python run.py --project <项目> --init --force")

    on_disk = set(chapter_folders_in_order())
    ready = [chapter["folder"] for chapter in skeleton
             if chapter["folder"] in on_disk]
    if ready:
        print(f"\n下一步:")
        print(f"  python run.py --project <项目> \"{ready[0]}\"   # 只跑第一章")
        print(f"  python run.py --project <项目> --all            # 按 outline 顺序跑全部")
        print(f"\n这些章都走【整篇模式】:知道自己是第几章,符号沿用前章,"
              f"跨章约定写在 {PAPER_ROOT}/cross-chapter-state.md。")
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
    from agents.outline import (OUTLINE_DRAFT_PATH, OUTLINE_PATH, OUTLINE_ZH_PATH,
                                OutlineRouteError, parse_outline)
    from agents.outline_expand import (
        EN_EXPANDED_MARKER, ZH_EXPANDED_MARKER, ZH_EXPANDED_PATH_NAME,
        build_expand_prompt, missing_word_counts, read_expanded,
        render_outline, skeleton_report, validate_expansion,
    )
    from config import IDEA_PATH

    # 输入是用户填的草稿(outline_draft.md),不是定稿(outline.md)。
    if not Path(OUTLINE_DRAFT_PATH).is_file():
        print(f"Error: 找不到 {OUTLINE_DRAFT_PATH}")
        print("先写一份章节骨架表(# | 章标题 | type),模板在 templates/outline_draft.md。")
        return 1

    try:
        chapters = parse_outline(OUTLINE_DRAFT_PATH)
    except OutlineRouteError as exc:
        print(f"Error: {exc}")
        return 1
    if not chapters:
        print(f"Error: {OUTLINE_DRAFT_PATH} 里没解析出任何章节。")
        print("需要一张 `# | 章标题 | type` 的表(或每章 `## N. 标题` + `type: <类型>`)。")
        return 1

    # idea.md 是展开的唯一依据。没有它,Manager 只能照章节标题编小节——那种
    # 结构对任何一篇论文都成立,也就对这一篇毫无价值,不如不跑。
    if not Path(IDEA_PATH).is_file():
        print(f"Error: 找不到 {IDEA_PATH}")
        print("展开小节要从你的创新点推导。先填 inbox/idea.md 的")
        print("第 3 节(核心洞察)与第 4 节(方法设计),再跑 --expand。")
        return 1

    en_path = Path(OUTLINE_PATH)
    zh_path = Path(OUTLINE_ZH_PATH)
    en_path.parent.mkdir(parents=True, exist_ok=True)
    # 旧产物必须先清掉。留着的话本次 Manager 写失败,下面"文件存在即成功"的判断
    # 会读到上一轮的内容并原样通过——作者拿着一份陈旧展开去 --init,而终端显示成功。
    for p in (en_path, zh_path):
        if p.exists():
            print(f"Error: {p.name} 已存在(上一轮 --expand 的产物)。")
            print(f"审阅 {ZH_EXPANDED_PATH_NAME} 后直接 --go;要重新展开就先删掉它们。")
            print(f"不自动覆盖:那份文件可能已经被你改过。")
            return 1

    print(f"Idea:    {IDEA_PATH}")
    print(f"\n你的章节骨架({len(chapters)} 章),Manager 不会改动它:")
    for line in skeleton_report(chapters):
        print(line)
    print(f"\nManager 将为每章补:小节拆分 + 每节的起草要点(含边界约束)。")
    print(f"字数不由它定 —— 展开后由你在小节标题后补 `(~N words)`。")
    print(f"产物中英双轨:英文版 {en_path}(喂 Agent)+ 中文版 {zh_path}(给你审阅)。\n")

    from config import get_manager_model
    from agents import create_planner_agent
    from agents.orchestrator import run_agent_stage_standalone

    print("Initializing manager model...")
    manager = create_planner_agent(get_manager_model())

    prompt = build_expand_prompt(chapters, str(en_path), str(zh_path),
                                 str(IDEA_PATH), len(chapters))
    run_agent_stage_standalone(manager, "Manager", prompt,
                               allowed_files=[en_path, zh_path])

    def _discard(reason_lines):
        """展开产物不合格:删掉(它们是机器刚生成、作者还没碰过的),免得下一次 --go 把失败
        产物当成“已审阅的大纲”继续往下跑。"""
        for line in reason_lines:
            print(line)
        for p in (en_path, zh_path):
            if p.exists():
                p.unlink()
        print("已删除不合格的展开产物;修好 idea.md / 骨架后重跑同一条命令会重新展开。")
        return 1

    missing_files = [p.name for p in (en_path, zh_path) if not p.is_file()]
    if missing_files:
        return _discard([f"\nError: 规划者没有写出 {', '.join(missing_files)}。"])

    # 英文版是喂给 Agent 的结构,以它为准做结构校验;中文版是给作者的对照。两者各自
    # 对骨架校验,再互相配对校验(章数 / 顺序 / type / 小节数 / 小节 type / 显式字数)。
    try:
        expanded_en = read_expanded(en_path)
        expanded_zh = read_expanded(zh_path)
    except OutlineRouteError as exc:
        return _discard([f"\nError: 展开产物解析不出章节: {exc}"])

    problems = validate_expansion(chapters, expanded_en)
    if problems:
        # 越权改结构必须拦。规划者悄悄合并两章或改掉一个 type,--init 照样会生成
        # 工作区,作者要到看见文件夹名不对才发现——那时 token 已经花了。
        return _discard([f"\nError: 英文大纲改动了你的章节结构,拒绝采纳:"] + [f"  · {x}" for x in problems])
    zh_problems = validate_expansion(chapters, expanded_zh)
    if zh_problems:
        return _discard([f"\nError: 中文大纲结构与骨架不一致,拒绝采纳:"] + [f"  · {x}" for x in zh_problems])
    from agents.outline_expand import validate_translation
    pair_problems = validate_translation(expanded_zh, expanded_en)
    if pair_problems:
        return _discard([f"\nError: 中英文大纲互相对不上,拒绝采纳:"] + [f"  · {x}" for x in pair_problems])

    # 重新序列化一遍再落盘:Manager 的原始输出可能夹带说明文字或代码围栏,
    # 而这份文件下一步要被作者改名成 outline.md,必须是干净的语法。
    title = f"Paper outline — {Path(os.environ.get('PAPER_PROJECT', 'paper')).name}"
    en_path.write_text(render_outline(expanded_en, title, EN_EXPANDED_MARKER),
                       encoding="utf-8")
    zh_title = f"论文大纲 — {Path(os.environ.get('PAPER_PROJECT', 'paper')).name}"
    zh_path.write_text(render_outline(expanded_zh, zh_title, ZH_EXPANDED_MARKER),
                       encoding="utf-8")
    from agents import project
    project.record_expansion()

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
    print(f"  1. 读 {zh_path}(中文版),改掉不认同的小节与要点,补上字数")
    print(f"  2. python run.py --project <项目> --go   # 审完再跑:自动翻译成英文大纲 → 逐章生成")
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


def _verification_tag(folder_path: str) -> str:
    """汇总表的核验后缀:未核验 → "·未核验";通过或无记录 → ""。"""
    from agents.orchestrator import read_verification_status
    return "·未核验" if read_verification_status(folder_path) == "unverified" else ""


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
        # "已完成"还要求 Stage 5 交接凭据与当前指纹相符:改了输入、删掉陈旧产物重跑到
        # Stage 4 后中断时,共享状态里还留着旧 claim,只看 final.md + claim 会把该章误判
        # 完成、永远跳过新的交接。凭据不符就照常跑这一章(前面产物都在,只会重跑
        # Stage 5;若产物陈旧,章内门禁会硬停)。
        from agents.orchestrator import chapter_staleness, handoff_is_current
        if chapter_is_complete(name) and handoff_is_current(folder_path):
            # 跳过前先验陈旧:改了 idea.md / data/ / 提示词 / outline 后,"已完成"的章
            # 是按旧输入写的。原先这里直接跳过,run_4stage 的指纹门禁根本走不到,
            # 改完 idea 再跑 --all 什么都不会发生。
            reasons = chapter_staleness(folder_path)
            if reasons:
                print(f"\n{'=' * 60}\n[{position}/{len(folders)}] {name} — 已完成但已陈旧\n{'=' * 60}")
                for line in reasons:
                    print(f"[run-all] 陈旧: {line}")
                print(f"[run-all] 停止:备份需要保留的内容后删除上述陈旧产物,再重跑 --all"
                      f"(不会自动删除任何文件)。")
                chapter_rows.append((position, name, "[陈旧] 需重跑"))
                _print_paper_table(folders, chapter_rows)
                return 1
            print(f"\n{'=' * 60}\n[{position}/{len(folders)}] {name} — 已完成,跳过\n{'=' * 60}")
            skipped.append(name)
            chapter_rows.append((position, name, f"[完成{_verification_tag(folder_path)}] 跳过"))
            continue

        print(f"\n{'=' * 60}\n[{position}/{len(folders)}] {name}\n{'=' * 60}")
        result = run_4stage_with_progress(
            draft_agent, review_agent, folder_path, manager_agent)

        if result.get("route_blocked"):
            # 门禁硬停(陈旧产物/数据歧义/brief 过期等):原因已在上方日志打印。
            # 单独处理,免得 final.md 已在时被误报成"跨章交接未完成"。
            chapter_rows.append((position, name, "[失败] 门禁硬停"))
            print(f"\n[run-all] 停止:{name} 被门禁拦下({result['route_blocked']})。")
            print(f"[run-all] 按上方日志处理后重跑 --all。")
            _print_paper_table(folders, chapter_rows)
            return 1
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
        chapter_rows.append((position, name, f"[完成{_verification_tag(folder_path)}]"))

    print(f"\n{'=' * 60}")
    print(f"[run-all] 全部完成:本次生成 {len(completed)} 章,跳过 {len(skipped)} 章。")
    unverified = [name for name in folders
                  if _verification_tag(os.path.join(PAPER_ROOT, name))]
    if unverified:
        print(f"[run-all] 其中 {len(unverified)} 章已生成但未通过证据核验"
              f"(原因见各章 verification.md / todo.md):")
        for name in unverified:
            print(f"[run-all]   - {name}")
    print(f"[run-all] 各章产物在 {PAPER_ROOT}/<章节>/final.md 与 final.zh.md")
    _print_paper_table(folders, chapter_rows)
    return 0


# ── 项目模式的新命令 ────────────────────────────────────────────────────
def cmd_new(args: list[str]) -> int:
    """`--new 名字 [--parent 目录]`:建项目文件夹和模板。"""
    from agents import project
    rest = [a for a in args if a != "--new"]
    parent = None
    if "--parent" in rest:
        i = rest.index("--parent")
        if i + 1 >= len(rest):
            print("Error: --parent 需要一个目录参数")
            return 2
        parent = Path(rest[i + 1]).expanduser()
        del rest[i:i + 2]
    if len(rest) != 1 or rest[0].startswith("--"):
        print("用法: python run.py --new 项目名 [--parent 目录]")
        return 2
    try:
        root = project.new_project(rest[0], parent)
    except (ValueError, FileExistsError) as exc:
        print(f"Error: {exc}")
        return 1
    print(f"已建好项目: {root}")
    print(f"""
下一步:
  1. 把材料放进 {root}/inbox/ (每个位置放什么见 inbox/说明.md):
       idea.md           创新点文档(按模板填,最重要)
       outline_draft.md  章节骨架表
       results/          主结果.csv 等结果表 + 实验设置.md
       references/       refs.bib + 每篇文献一个文件夹(笔记 + 原文 md)
       code/             实验代码(可选)
  2. python run.py --project {root} --check   # 先看材料有没有问题(不调模型)
  3. python run.py --project {root} --go      # 展开大纲后停下,等你审 outline.zh.md
  4. 审完再跑一次 --go,逐章生成并组装到 {root}/output/
模型 key 填在仓库根目录的 .env(cp .env.example .env);也可以放项目里的 .env 覆盖。""")
    return 0


def _write_report(name: str, text: str) -> Path:
    import config
    path = Path(config.OUTPUT_ROOT) / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def cmd_check(need_models: bool = False) -> int:
    from agents import project
    chk = project.check_materials(need_models=need_models)
    path = _write_report("材料检查.md", chk.render())
    for e in chk.errors:
        print(f"[错误] {e}")
    for w in chk.warnings:
        print(f"[提醒] {w}")
    for i in chk.info:
        print(f"[概况] {i}")
    print(f"\n检查报告: {path}")
    if not chk.ok:
        print("有必须先修的问题,修好后重跑。")
        return 1
    print("材料检查通过。")
    return 0


def _indexes_or_report():
    """建索引;结果表有歧义时给友好提示而不是 traceback。成功返回概况,失败返回 None。"""
    from agents import project
    from agents.number_gate import ResultsStoreError
    try:
        return project.build_all_indexes()
    except ResultsStoreError as exc:
        print("Error: 结果表有问题,不能建数据索引:")
        for line in str(exc).splitlines():
            print(f"  {line}")
        return None


def cmd_index() -> int:
    info = _indexes_or_report()
    if info is None:
        return 1
    import config
    print(f"索引已建在 {config.INDEX_DIR}: 文献 {info['references']} 条,结果数值 {info['results']} 个")
    return 0


def cmd_assemble() -> int:
    """重新核验各章终稿(不调模型)并组装 output/。手改过终稿后用它。"""
    from agents import project
    from agents.orchestrator import reverify_chapter
    from agents.outline import parse_outline, OutlineRouteError
    import config
    try:
        folders = [c["folder"] for c in parse_outline()]
    except OutlineRouteError as exc:
        print(f"Error: 无法读 outline: {exc}")
        return 1
    flipped = []
    for name in folders:
        folder = os.path.join(config.WORKSPACE_ROOT, name)
        before = _read_status(folder)
        result = reverify_chapter(folder)
        if result.get("status") in ("verified", "unverified") and result["status"] != before and before:
            flipped.append(f"{name}: {before} → {result['status']}")
    for line in flipped:
        print(f"[重新核验] {line}")
    try:
        info = project.assemble_output()
    except ValueError as exc:
        print(f"Error: {exc}")
        return 1
    if info["missing"]:
        print(f"[缺章] 以下章没有终稿,paper.md / LaTeX 里是占位提示: {', '.join(info['missing'])}")
    print(f"\n已组装到 {info['out']}: {info['with_final']}/{info['chapters']} 章有终稿,"
          f"{info['unverified']} 章未核验,{info['tables']} 张结果表,{info['figures']} 张待补图")
    if info["latex_checks"]:
        print("LaTeX 静态检查发现:")
        for p in info["latex_checks"]:
            print(f"  · {p}")
    print("交付物: paper.md / paper.zh.md / latex/ / 待补清单.md / 图表清单.md / 数字溯源表.md")
    return 0


def _read_status(folder: str) -> str:
    from agents.orchestrator import read_verification_status
    try:
        return read_verification_status(folder)
    except Exception:
        return ""


def cmd_go(keep_outline: bool) -> int:
    """一条命令:材料检查 → 建索引 → 展开大纲(首次停下等审) → 逐章生成 → 组装。"""
    import config
    from agents import project
    from agents.outline import OUTLINE_ZH_PATH

    print("== 1/5 材料检查 ==")
    chk = project.check_materials(need_models=True)
    report = _write_report("材料检查.md", chk.render())
    for e in chk.errors:
        print(f"[错误] {e}")
    for w in chk.warnings:
        print(f"[提醒] {w}")
    if not chk.ok:
        print(f"\n有必须先修的问题(详见 {report}),修好后重跑同一条命令。")
        return 1

    print("\n== 2/5 建索引 ==")
    info = project.build_all_indexes()
    print(f"文献 {info['references']} 条,结果数值 {info['results']} 个 → {config.INDEX_DIR}")

    print("\n== 3/5 大纲 ==")
    zh = Path(OUTLINE_ZH_PATH)
    if not zh.is_file():
        rc = expand_outline()
        if rc != 0:
            return rc
        print(f"\n大纲已展开。请审阅并按需修改:\n  {zh}\n改完后重跑同一条命令继续。")
        return 0
    if not project.load_state().get("zh"):
        print(f"{zh} 存在,但没有“展开成功”的记录(可能是上次展开失败或手工放的文件)。")
        print(f"请删掉 {zh} 和 {config.WORK_ROOT}/outline.md 后重跑同一条命令重新展开。")
        return 1
    changed = project.outline_inputs_changed()
    if changed:
        if not keep_outline:
            print(f"{'、'.join(changed)} 在展开大纲之后改过,大纲里的小节要点可能已和它们不一致。")
            print("二选一:")
            print(f"  · 删掉 {zh} 和 {config.WORK_ROOT}/outline.md,重新展开(你手改过的大纲会丢);")
            print("  · 确认继续沿用现有大纲:重跑并加 --keep-outline。")
            return 1
        state = project.load_state()
        state.update(project.input_shas())
        project.save_state(state)
        print(f"已确认继续沿用现有大纲({'、'.join(changed)} 的改动不再提醒)。")
    if _translate_zh_to_en() != 0:
        return 1
    rc = init_workspaces(force=False)
    if rc != 0:
        return rc

    print("\n== 4/5 逐章生成 ==")
    print("Initializing models...")
    manager, draft_agent, review_agent = create_agents(
        get_draft_model(), get_review_model(), get_manager_model())
    rc = run_all_chapters(draft_agent, review_agent, manager)
    if rc != 0:
        print("\n某章失败或被拦下,已停止。修好后重跑同一条命令会从这一章继续。")
        return rc

    print("\n== 5/5 核验并组装 ==")
    return cmd_assemble()


# ── 入口 ────────────────────────────────────────────────────────────────
USAGE = """用法:
  python run.py --new 项目名                          建项目 + 模板
  python run.py --project <项目> --go                 一条命令出初稿(首次展开大纲后停下等你审)
  python run.py --project <项目> --check | --index | --expand | --init | --all | --assemble | --list
  python run.py --project <项目> <章节名>             只跑一章
详见 README.md。"""


def main():
    argv = sys.argv[1:]
    if not argv or argv[0] in ("-h", "--help"):
        print(USAGE)
        return

    if "--new" in argv:
        sys.exit(cmd_new(argv))

    import config
    if not config.HAS_PROJECT:
        print("Error: 需要 --project <项目路径>(还没有项目先 `python run.py --new 项目名`)。")
        print(USAGE)
        sys.exit(2)
    if not Path(config.PROJECT_ROOT).is_dir():
        print(f"Error: 项目文件夹不存在: {config.PROJECT_ROOT}")
        sys.exit(2)

    rest = [a for a in argv if a not in ("--project", "--progress")]
    if "--project" in argv:
        i = argv.index("--project")
        if i + 1 >= len(argv) or argv[i + 1].startswith("--"):
            print("Error: --project 后面需要项目路径")
            sys.exit(2)
        rest = [a for j, a in enumerate(argv) if j not in (i, i + 1) and a != "--progress"]

    actions = ("--check", "--index", "--assemble", "--go", "--list", "--init", "--expand", "--all")
    known = set(actions) | {"--keep-outline", "--force"}
    unknown = [a for a in rest if a.startswith("--") and a not in known]
    if unknown:
        print(f"Error: 未知参数 {' '.join(unknown)}")
        print(USAGE)
        sys.exit(2)
    chosen = [a for a in actions if a in rest]
    names_given = [a for a in rest if not a.startswith("--")]
    if len(chosen) > 1 or (chosen and names_given):
        print(f"Error: 一次只能做一件事,收到: {' '.join(chosen + names_given)}")
        sys.exit(2)
    if "--keep-outline" in rest and "--go" not in rest:
        print("Error: --keep-outline 只能和 --go 一起用")
        sys.exit(2)
    if "--force" in rest and "--init" not in rest:
        print("Error: --force 只能和 --init 一起用")
        sys.exit(2)

    if "--check" in rest:
        sys.exit(cmd_check())
    if "--index" in rest:
        sys.exit(cmd_index())
    if "--assemble" in rest:
        sys.exit(cmd_assemble())
    if "--go" in rest:
        sys.exit(cmd_go(keep_outline="--keep-outline" in rest))
    if "--list" in rest:
        list_paper_folders()
        return
    if "--init" in rest:
        sys.exit(init_workspaces(force="--force" in rest))
    if "--expand" in rest:
        sys.exit(expand_outline())

    run_all = "--all" in rest
    names = [a for a in rest if not a.startswith("--")]
    if not run_all and len(names) != 1:
        print(USAGE)
        print("\n现有章节:")
        list_paper_folders()
        sys.exit(2)

    folder_name = None if run_all else names[0]
    folder_path = None
    if not run_all:
        folder_path = os.path.join(PAPER_ROOT, folder_name)
        if folder_name.startswith("_"):
            print(f"Error: '{folder_name}' 是脚手架目录(`_` 前缀),不是可跑的章节。")
            sys.exit(1)
        if not os.path.isdir(folder_path):
            print(f"Error: folder '{folder_path}' does not exist.")
            print("现有章节:")
            list_paper_folders()
            print("\n从大纲生成章节文件夹: python run.py --project <项目> --init")
            sys.exit(1)
        # 先确认这一章在 outline 里。不在 outline 的文件夹不是合法章节——即使它
        # 也缺 brief.md,首要错误仍是路由非法,不该报成"缺 brief"。
        from agents.outline import resolve_write_mode, OutlineRouteError
        try:
            info = resolve_write_mode(folder_name)
        except OutlineRouteError as exc:
            print(f"\nError: '{folder_name}' 不是合法章节: {exc}")
            sys.exit(1)
        if not os.path.exists(os.path.join(folder_path, "brief.md")):
            print(f"Error: {folder_path}/brief.md not found.")
            print("从大纲生成: python run.py --project <项目> --init")
            sys.exit(1)
        print(f"\n路由:outline 的第 {info['position']}/{info['total']} 章")
        print(f"  · 可跨章引用,符号沿用前章(见 {PAPER_ROOT}/cross-chapter-state.md)")

    # 单章 / --all 之前也要有索引(文献索引缺失会让备料直接失败)。
    if _indexes_or_report() is None:
        sys.exit(1)

    print("Initializing models...")
    manager, draft_agent, review_agent = create_agents(
        get_draft_model(), get_review_model(), get_manager_model())
    if run_all:
        print(f"\n{'=' * 60}\n整篇模式:按 outline 的顺序跑完 {PAPER_ROOT} 下的各章\n{'=' * 60}")
        sys.exit(run_all_chapters(draft_agent, review_agent, manager))
    print(f"\n{'=' * 60}\nRunning 4-stage iteration on: {folder_name}\n{'=' * 60}")
    results = run_4stage_with_progress(draft_agent, review_agent, folder_path, manager)
    print(f"\nStages completed: {len(results)}")


if __name__ == "__main__":
    main()
