# Paper outline — <论文标题>

<!-- 复制成 outline.md,把下面表格的占位符换成你的章节。只填这张表就够了。 -->

| # | 章标题 | type |
|---|--------|------|
| 1 | <章标题,如 Abstract> | <见下方词表,如 abstract> |
| 2 | <章标题> | <type> |
| 3 | <章标题> | <type> |
| 4 | <章标题> | <type> |
| 5 | <章标题> | <type> |
| 6 | <章标题> | <type> |
| 7 | <章标题> | <type> |

<!--
分几章、每章叫什么、什么 type、什么顺序 —— 全由你定,加减行即可。
章标题中英文都行(决定文件夹名)。type 必须从下面词表取值。

填完跑:
  python run.py --expand    # 规划者读 idea.md,补每章的小节与要点 → outline.expanded.md
                            #   审阅它,补上 (~N words) 定篇幅,改名成 outline.md
  python run.py --init      # 生成各章工作区

只填这张表就直接 --init 会被拒绝:没有小节的章,起草时拿不到任何要点,
起草者只能照章标题硬编。必须先 --expand(或自己写 ### 小节)。
-->

## type 词表

| type | 主输入 | 数字门禁 | 中文别名 |
|------|--------|----------|----------|
| `abstract` | idea.md | advisory | 摘要 |
| `intro` | idea.md | advisory | 引言 / 绪论 |
| `related` | idea.md + 文献 | off | 相关工作 |
| `background` | idea.md + 文献 | off | 背景 / 预备知识 |
| `method` | idea.md | advisory | 方法 / 模型 |
| `theory` | idea.md | advisory | 理论 |
| `experiments` | data/ | blocking | 实验 / 实验设置 |
| `results` | data/ | blocking | 结果 / 评估 |
| `analysis` | data/ | blocking | 分析 |
| `ablation` | data/ | blocking | 消融 |
| `discussion` | 两者 | advisory | 讨论 |
| `limitations` | 两者 | advisory | 局限 |
| `conclusion` | 两者 | advisory | 结论 / 总结 |

- **主输入** — 这章从哪儿取材。idea 类章节以 `idea.md` 为主,data 类以 `data/` 为主。
- **数字门禁** — `blocking` 表示 `data/` 为空时拒绝起草;`advisory` 表示照写、数字标 UNVERIFIED;`off` 表示不查。
