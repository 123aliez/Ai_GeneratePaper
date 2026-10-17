# Paper outline — <论文标题>

<!-- 放在 inbox/outline_draft.md。只填下面这张表:分几章、每章叫什么、什么 type、什么顺序。
     加减行即可;章标题中英文都行;type 必须取自本文件末尾的词表。
     小节和起草要点不用你写:`python run.py --project <项目> --go` 会让规划者读 idea.md 展开,
     生成 outline.zh.md,你审完/改完再跑一次 --go。 -->

| #   | 章标题               | type         |
| --- | ------------------- | ------------ |
| 1   | Abstract            | abstract     |
| 2   | Introduction        | intro        |
| 3   | Related Work        | related      |
| 4   | Method              | method       |
| 5   | Experimental Setup  | experiments  |
| 6   | Results             | results      |
| 7   | Ablation Study      | ablation     |
| 8   | Conclusion          | conclusion   |

## type 词表

| type | 主输入 | 数字门禁 | 中文别名 |
|------|--------|----------|----------|
| `abstract` | idea.md + 结果表 | advisory | 摘要 |
| `intro` | idea.md + 结果表 | advisory | 引言 / 绪论 |
| `related` | idea.md + 文献笔记 | off | 相关工作 |
| `background` | idea.md + 文献笔记 | off | 背景 / 预备知识 |
| `method` | idea.md | advisory | 方法 / 模型 |
| `theory` | idea.md | advisory | 理论 |
| `experiments` | 实验设置.md + 结果表 | blocking | 实验 / 实验设置 |
| `results` | 结果表 | blocking | 结果 / 评估 |
| `analysis` | 结果表 | blocking | 分析 |
| `ablation` | 结果表 | blocking | 消融 |
| `discussion` | 两者 | advisory | 讨论 |
| `limitations` | 两者 | advisory | 局限 |
| `conclusion` | 两者 | advisory | 结论 / 总结 |

- **数字门禁** — `blocking`:结果目录里没有结果表就拒绝起草(这类章节的内容就是数字);
  `advisory`:照写,数字标 UNVERIFIED;`off`:不查。
- 摘要 / 引言可以引用 headline 结果,但数字一律走 `{{RES-xxx}}` 占位符,由程序替换。
