# Brief: Chapter <N> — <章节名>

> **这份模板是给【逐章模式】用的** —— 你自己建一个文件夹、自己写 brief,只生成这一章:
>
> ```bash
> mkdir -p workspace/my-chapter
> cp workspace/_TEMPLATE/brief.md workspace/my-chapter/brief.md
> # 编辑本文件,再写 workspace/my-chapter/input.md 放素材
> python run.py my-chapter --progress
> ```
>
> 这条路由下,本章按**自包含**写:所有术语与符号在本章首次出现处定义,
> **严禁**写 "as shown in Section 3"、"we introduced X earlier"、"the next chapter evaluates"
> 这类指向不存在章节的过渡句(那是悬空引用)。不读也不写跨章状态。
>
> **要写一整篇论文,不要用这份模板** —— 改写项目根的 `outline.md`,然后
> `python run.py --init` 会为每一章自动生成 brief。那条路由下每章知道自己是第几章、
> 可跨章引用、符号沿用前章。判据只有一条:该文件夹在不在 `outline.md` 里。
>
> 格式约定:框架解析每个编号 `**Title** (~N words)` 行当作小节,
> 起草段数 = `min(小节数, 3)`。

## Type

type: <method>

> **必填。这一行决定这章从哪儿取材,以及数字门禁怎么管。**
>
> | 写这个 | 主输入 | 数字门禁 | 用于 |
> |---|---|---|---|
> | `abstract` | idea.md | advisory | 摘要 |
> | `intro` | idea.md | advisory | 引言 |
> | `related` | idea.md + 文献 | off | 相关工作 |
> | `background` | idea.md + 文献 | off | 背景/预备知识 |
> | `method` | **idea.md** | advisory | 方法(论文核心) |
> | `theory` | idea.md | advisory | 理论分析/证明 |
> | `experiments` | **data/** | blocking | 实验设置 |
> | `results` | **data/** | blocking | 结果 |
> | `analysis` | data/ | blocking | 实证分析 |
> | `ablation` | data/ | blocking | 消融实验 |
> | `discussion` | 两者 | advisory | 讨论 |
> | `limitations` | 两者 | advisory | 局限 |
> | `conclusion` | 两者 | advisory | 结论 |
>
> - **主输入**:idea 类章节以你写的 `idea.md`(创新点/原理/方法设计)为主,结果表只作辅助证据;
>   data 类章节反过来。这是因为论文的贡献是创新点,实验数据只是支撑。
> - **数字门禁**:`blocking` = 没有 `data/` 结果就拒绝起草(这类章节的内容就是数字);
>   `advisory` = 有结果就比对、没结果不拦(方法章可以先于实验写);`off` = 不跑(纯论述章)。
> - 中文别名也认(`方法`/`结果`/`消融`…)。写错会退回 mixed/advisory 并在终端提示。
> - **本章涵盖多种类型的小节时**:在这里写主类型,然后在具体小节下写 `- type: results`
>   覆盖该节。框架按小节和按起草段分别路由,idea 段用 `idea.md`、data 段用 `data/`,互不干扰。

## Task
<这一章要做什么:一句话说清这章回答什么问题>

## Expected Output
<这一章的产出物,通常是:>

1. **<小节1标题>** (~500 words)
- <要点:这节覆盖什么、关键论断、需要的证据/数据>

2. **<小节2标题>** (~500 words)
- <要点>

3. **<小节3标题>** (~500 words)
- <要点>

## Notes for the Drafter
- **创新点/原理**:来自 `idea.md`(见 `idea.example.md` 模板)。缺的标 [DESIGN DETAIL NEEDED]
- **数字**:每个数字必须来自 data/ 结果库(见 data/README.md),缺的标 [MISSING DATA]
- **公式**:用 LaTeX 语法,符号遵守 Notation Table(见 math-formula.md)
- **图表**:不生成图,写占位符 + 说明(见 figure-table-placeholder.md)
- **文献**:用 references/bibliography.md 的 REF-ID,不编造(见写作规范)
- 任何证据不足的论断标 [CITATION NEEDED] 或 [DATA NEEDED],绝不猜

> **章节衔接不用你在这里交代。** 这份 brief 走逐章模式,写作契约由框架注入:
> 本章自包含,不写跨章过渡句。要写有前后衔接的整篇论文,改用 `outline.md` + `--init`。
