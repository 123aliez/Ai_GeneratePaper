# Experiment-Paper Writing — 实验论文写作专属规范

> 起草实验论文时按需读取。这是本框架的核心规范,统领其他 skill。

## 两个源,先认清你在写哪类章节

**论文的贡献是创新点和原理,实验数据只是支撑证据。** 起草前先看提示词里的
`CHAPTER TYPE` 子句,它告诉你这段属于哪一类:

| family | 主源 | 另一个源的角色 |
|---|---|---|
| `idea` | **`## Core idea` 块**(作者写的 `idea.md`) | 结果表只作辅助,最多引一个 headline 数 |
| `data` | **结果表**(`data/` 结果库) | Core idea 只用来决定narrative,不重述方法设计 |
| `mixed` | 两者并重 | 论断来自 Core idea,支撑来自结果表 |

**不许跨类型代偿**:方法章不许用实验结果代替机制解释("我们的方法涨了 3.4 点"
不是"为什么这样设计能work"的答案);结果章不许用方法阐述填篇幅。

## 创新点纪律(idea 类章节最高优先)
- **创新点、机制、方法设计只能来自 `## Core idea` 块**,那是作者自己的表述
- 不许**弱化**也不许**放大**作者的论断;贡献清单以外的贡献一律不许声称
- Core idea 没写到的设计细节标 `[DESIGN DETAIL NEEDED]`,**绝不"合理地"补一个**
- 整份创新点文档缺失时标 `[IDEA NEEDED]`——框架会拦住,不该走到起草这步
- 解释机制要回答"为什么work",不是"效果多好"

## 数字纪律(data 类章节最高优先)
- **每个数字必须来自 data/ 结果库**,能追溯到 context-pack 的结果表
- 引用数字必须带来源:如 `run_1_ours 的 top1_accuracy = 81.7%`
- 缺数据标 `[MISSING DATA]`,绝不猜、绝不 extrapolate
- 结果与讨论必须引**同一组数**——同一指标在不同章节值不能变
- idea 类章节引 headline 数时同样受这条约束:不在结果表里的数字一律不许写

## 章节内容边界
| 章节 | type | 只写什么 |
|---|---|---|
| Abstract | `abstract` | 问题+方法+最强量化结果(数字必须在结果表里) |
| Introduction | `intro` | 动机+贡献清单(**照 Core idea 的清单,不加不减**),每个贡献要有后续支撑 |
| Related Work | `related` | 用你的 bibliography + Core idea 的 delta 表;不凭记忆说别人数字 |
| Background | `background` | 预备知识和记号;不掺入本文贡献 |
| Method | `method` | **从 Core idea 展开机制与设计选择**;只写真正实现的;遵守 Notation Table |
| Theory | `theory` | 从 Core idea 的公式出发;假设要写全 |
| Experimental Setup | `experiments` | 只写结果库/hardware 里有的;没有标 [UNKNOWN] |
| Results | `results` | 只写结果库里的;表格用 3+ 对比;禁幻觉;不重述方法 |
| Ablation | `ablation` | 逐个组件对照;缺的消融标 gap 不编 |
| Discussion | `discussion` | 只解释已报告的结果;同一组数;区分 supported vs suggests |
| Limitations | `limitations` | 以 Core idea 的「已知局限」为底,加数据暴露的短板 |
| Conclusion | `conclusion` | 只总结已demonstrated的;无新数字 |

## 与占位符/公式结合
- 结果里的图表:按 `figure-table-placeholder.md` 写占位符 + 说明,不生成图
- 公式:按 `math-formula.md` 用 LaTeX 语法,符号遵守 Notation Table
- 方法章节的符号必须和 Notation Table 一致,不重定义
- Core idea 里作者已定义的符号**直接沿用**,不另起一套记号

## 防幻觉铁律
- 不臆想**创新点或机制**(Core idea 里没有的 → `[DESIGN DETAIL NEEDED]`)
- 不臆想硬件/超参/数据集大小(结果库里没有的 → `[UNKNOWN]`)
- 不臆想未跑的实验/消融(缺失 baseline → 标 gap,不编)
- 不臆想显著性(没有检验/区间数据 → 只描述趋势,不 assert significance)
- 答不出就说 "I cannot answer from the provided evidence",不留空档硬编

## 写完后自检
1. 这段的 `CHAPTER TYPE` 是什么?主源用对了吗?
2. idea 类:机制是**解释**了还是只**断言**了?设计选择给理由了吗?
3. idea 类:声称的贡献都在 Core idea 的贡献清单里吗?
4. data 类:每个数字都能对回 data/ 结果库?
5. Results 和 Discussion 引同一组数?
6. 每张图占位符都有数据来源 + 支撑论断?
7. 每个符号首次使用都定义了,且与 Core idea 一致?
