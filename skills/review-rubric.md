# Review Rubric — 审稿规范

> Review Agent 评审时按需读取。源自 academic-paper-reviewer + AI-Research-SKILLs 精华提炼。

## 先认清章节类型,再套判据(最重要)

提示词里有一句 `This is a <type> chapter`,它决定你用哪套标准。**套错标准的代价很大**:
你的 MUST FIX 清单会被冻结成验收单,Draft 要花最多 4 轮去满足它——如果里面塞的是
这章本来就不该报的东西,那 4 轮全是白烧。

| chapter family | 判什么 | **明确不许要求什么** |
|---|---|---|
| `idea`(method/intro/related/abstract/theory) | 机制是否**解释**了而非只**断言**;设计选择有无理由;是否跑偏成结果复述 | **不许索要更多实验数字、统计显著性、baseline 对比**。缺指标不是缺陷,那是结果章的事 |
| `data`(results/experiments/analysis/ablation) | 每个数字能否对回结果表;baseline 是否公平且 current;方差/种子有无报;结论是否强于数据 | **不许索要方法设计阐述**。那是方法章的事 |
| `mixed`(discussion/limitations/conclusion) | 每条关于贡献的论断有无具体数字支撑;有无引入作者没声称过的贡献 | — |

数字门禁级别也在提示词里:
- `advisory` → 结果库为空**本身不是缺陷**(方法章可以先于实验写)
- `off` → 这章是纯论述,不报任何指标是**正确的**

## 审稿五维加权打分
| 维度 | 权重 | 问什么 |
|---|---|---|
| Originality | 20% | 贡献是否新颖;idea 章:是否忠实于 Core idea 且没超出贡献清单 |
| Rigor | 25% | 方法是否严谨、假设是否声明;idea 章:因果链是否完整 |
| Evidence | 25% | 证据是否支撑结论;data 章:数字是否对账结果库 |
| Coherence | 15% | 论证链是否连贯 |
| Writing | 15% | 表达是否清晰、符合规范 |

分数映射:≥80 Accept / 65-79 Minor / 50-64 Major / <50 Reject

## 每条批评必须三要素
**what's wrong + where(具体位置)+ how to fix(具体改法)**。
禁止:fabricating review comments、generic feedback("could be improved")、sycophantic score inflation(无根据给高分)、**向本类型不该负责的内容索要证据**。

## 创新点忠实度检查(idea 类章节关键)
逐条对照 `idea.md`(作者的全局文档,读全文):
- 正文声称的贡献,是否都在 idea.md 的贡献清单里? → 超出的**必须** MUST FIX
- 核心洞察被弱化或改写了吗? → 改写作者的论断是 MUST FIX
- 机制只说了"做什么",没说"为什么work"? → MUST FIX(这是 idea 章的核心失效模式)
- 关键设计选择没给理由? → MUST FIX,但如果 idea.md 本身没写,**标给作者**而不是让 Draft 编
- 出现 `[DESIGN DETAIL NEEDED]` 标记? → **这是正确行为**,不是缺陷。转成给作者的待办

## 数据-结论 mismatch 检查(data 类章节关键)
每查一个论断,核对它对应的数据:
- 数据 p=.12 却说"显著改善" → **必须** MUST FIX
- 声称"基线公平"但没对比结果 → 标记证据缺失
- 正文出现结果表里没有的数字 → **必须** MUST FIX(number-check.md 会先机械抓一遍)
- 声称类型↔证据设计匹配:
  - 因果声称 → 需消融实验
  - 改进声称 → 需基线对比
  - 泛化声称 → 需异质测试

## Devil's Advocate 挑战
每篇至少挑战一次"最能证明贡献的点",但挑战点要落在本章类型能负责的范围内:
- idea 章:核心洞察的因果链是否断裂?有没有更简单的解释能达到同样效果?
  这个机制是否只是**换了个说法**描述已知现象?
- data 章:基础是否崩塌(数据与结论不符)?是否存在更强的反叙事?
- 通用:过 "So What?" 测试——这个贡献真有人在意吗?

## 防谄媚(Anti-Sycophancy)
- 分数一旦给出,不得随意软化
- 被反驳后需有真实依据才让步
- 连续让步后,再通过的门槛应升高

## 输出顺序
按严重度排序:critical / major / minor / suggestion,每条带具体位置 + 证据定位 + 改法。不挑错式评审,每处建议必须建设性。

## 与 review-v1.json 的关系
本规范指导 Markdown 评审(review-v1.md)的内容质量;结构化 JSON(review-v1.json)承载 must_fix 清单 + needs_citation,供编排层消费。
