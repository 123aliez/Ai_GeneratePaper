# Academic Writing Style — 写作规范

> 起草论文正文时按需读取。源自 academic-research-skills + AI-Research-SKILLs 精华提炼。

## 段落结构:TEEL
每段由四部分构成:
- **Topic** — 主题句,亮出本段主论断
- **Evidence** — 支撑证据。**data 类章节**是具体数字和结果;**idea 类章节**(方法/引言/
  相关工作)是机制论证、设计理由、与前人的具体差异——不是硬塞一个指标进来
- **Explanation** — 解释证据如何支撑论断
- **Link** — 连接下一段

解决:段落散乱无主句、平均用力。

## 禁词表 + 节奏控制
- 禁词:delv(e)/crucial/tapestry/landscape/a testament to/in the realm of/in recent years(除非给年份)/it is worth noting
- em dash(—)每篇 ≤3 个;semicolon ≤2/1000 词
- 删除"喉清式"开头:"In this section we will discuss..."、"It is important to note that..."
- 段落长度 2-8 句,长短错落,勿千篇一律

## 同义词固定原则
同一概念在一节内固定用一个词(技术性重复是优点,不是缺点)。"fine-tuning"就一直是"fine-tuning",不换成"finetuning"或"tuning"。

## 分节时态表
| 章节 | 时态 |
|---|---|
| 文献综述 | 过去时 |
| 方法 | 过去时 |
| 结果 | 过去时 |
| 讨论/结论 | 现在时 |

## Hedging 分级
- **may** < **suggests** < **indicates** < **demonstrates** — 证据越强,用词越确定
- 报告确凿数据时**不 hedging**:别写"accuracy appeared to be 78.3%",就写"accuracy was 78.3%"
- 实验论文最忌过度声明:数据说 p=.12 就别写"显著改善"

## 摘要 5 句式公式
1. **What** — 什么问题
2. **Why hard** — 为什么难
3. **How** — 怎么做(照 `idea.md` 的一句话贡献,不自己重新概括)
4. **Evidence** — 证据(最亮的一个数字;这个数**必须**在 data-index.md 里,没有就先留 [MISSING DATA])
5. **So what** — 意义

删掉"任何论文都能开头"的套话。

## Clarity Test
写完每段自问:**删掉它,论文还成立吗?** 若成立,该段信息冗余。读者应能随时回答"我在哪 / 为何在此 / 带走什么 / 下一站"。

## 句子级微技巧
- 代词必带名词:"This result shows..."(非"This shows...")
- 删 filler:actually/very/quite/really
- 空泛词换具体值:"large model"→"a 1B-parameter model"
