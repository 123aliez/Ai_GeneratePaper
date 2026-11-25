# Academic Writing Style — 写作规范

> 起草论文正文时按需读取。源自 academic-research-skills + AI-Research-SKILLs 精华提炼。

## 核心表达铁律（最高优先级）
- **清晰简单，绝不让读者猜 Idea**：开门见山说明核心想法与方法流程，避免绕弯、隐晦或故作高深的表达。
- **一词胜两词**：一个词能准确表达就绝不用两个词；优先使用简单精准的动词和直白短句，砍掉无信息量的修饰语和冗余从句。
- **零证明、零推导**：只保留最终可用公式与直白直觉，不出现任何数学证明与分步推导过程。

## 段落结构:按论证需要组织
TEEL 是论证段可用的检查工具，不是每段必须填满的四格模板:
- **Topic** — 主题句,亮出本段主论断
- **Evidence** — 支撑证据。**data 类章节**是具体数字和结果;**idea 类章节**(方法/引言/
  相关工作)是机制论证、设计理由、与前人的具体差异——不是硬塞一个指标进来
- **Explanation** — 解释证据如何支撑论断
- **Link** — 必要时连接下一段，内容自然衔接即可，不另补一句导航话

定义段、方法解释、结果解读各有用途，不为凑齐模板重复证据和解释。先判断段落是否需要存在，再润色句子。

## 禁词表 + 节奏控制
- 禁词:delv(e)/crucial/tapestry/landscape/a testament to/in the realm of/in recent years(除非给年份)/it is worth noting
  /pivotal/intricate/meticulous/underscore/nuanced/seamless/transformative/comprehensive(除非真指"覆盖全部")/notably
- 模糊修饰词(significant/substantial/remarkable/robust)只在有统计检验或具体数字时用;否则删,或换成可度量的描述
- 连接词(Moreover/Furthermore/Additionally)不要成串;句间关系靠内容本身(因果、对比、递进、举例),
  需要时写明关系,不靠连接词凑数
- em dash(—)每篇 ≤3 个;semicolon ≤2/1000 词
- 删除"喉清式"开头:"In this section we will discuss..."、"It is important to note that..."
- 段落长度 2-8 句,长短错落,勿千篇一律

## 句子与段落的读者友好写法(Gopen & Swan / Lipton)
- 主语与谓语靠近;旧信息在前、新信息在后;句子的重点放在句尾
- 用动词不用名词化:"We analyzed X",不写 "We performed an analysis of X"
- 一段只讲一件事,**主题句放第一句**;词要具体("accuracy"/"latency",不写 "performance")
- 删强调词(very/quite/basically/essentially)和无信息的 hedge;只有真不确定才用 may/can
- 写完做一次**反向大纲**:逐段摘出主题句,若某段的主题句串不进本章论点,删或并

## 同义词固定原则
同一概念在一节内固定用一个词(技术性重复是优点,不是缺点)。"fine-tuning"就一直是"fine-tuning",不换成"finetuning"或"tuning"。

## 分节时态表(全文统一,不要同一章内来回切换)
| 章节 | 时态 |
|---|---|
| 摘要 / 引言 | 现在时(写做了什么、提出什么);回顾他人工作用过去时 |
| 文献综述 | 他人具体研究用过去时("He et al. introduced ..."),通用事实用现在时 |
| 方法 | 现在时("The module computes ...")——机制是现在仍成立的描述 |
| 实验设置 | 现在时("Training runs for 100 epochs.") |
| 结果 | 过去时("SE lowered top-1 error ...") |
| 讨论/结论 | 现在时 |
引用其他部分写 `[[SEC:<大纲里的标题原文>]]`,程序替换成 "Section 4" / "Section 4.2";
不要自己写节号,也不写 "the method chapter" 或裸章名("in Results")。每段最多一处,真需要才写。
正文里不出现工作流用语:不写 supplied / provided / attached 的"材料 / 索引 / 笔记 / 大纲",
直接陈述事实本身,不交代事实从哪份输入来。

## Hedging 分级
- **may** < **suggests** < **indicates** < **demonstrates** — 证据越强,用词越确定
- 报告确凿数据时**不 hedging**:别写"accuracy appeared to be 78.3%",就写"accuracy was 78.3%"
- 实验论文最忌过度声明:数据说 p=.12 就别写"显著改善"

## 摘要硬性结构（整段，最多 7 句）
- **第 1 句**：交代本文背景。
- **第 2 句**：交代本文要解决的核心问题。
- **第 3–4 句**（中段总计可 2–3 句，但全段总句数严格 $\le 7$ 句）：直接交代本文提出的方法与核心贡献（照 `idea.md` 第 6 节，不让读者猜测）。
- **最后 1 句**：交代本文最关键的实验结果（必须写成数据索引里的 `{{RES-xxx}}` 占位符，缺失标 `[MISSING DATA]`）。

**形式硬性要求**：摘要只能是**一整段**，严格 $\le 7$ 句；不分段、不列表、不加小标题、不放引用；用词极简，不逐格复述结果表。

## Clarity Test
写完每段自问：它是否帮助理解机制、评价证据、复现核心设计或限定结论？若无独立作用则删并。
不能只因删后主结论仍成立就删除必要的定义、反例、不确定性和比较条件。

## 句子级微技巧
- 代词必带名词:"This result shows..."(非"This shows...")
- 删 filler:actually/very/quite/really
- 空泛词换具体值:"large model"→"a 1B-parameter model"

## 简洁(防啰嗦)
- 一句话一个论断；内部来源与核验说明不写进正文，不反复用 reported / supplied / displayed 铺垫。科学条件在首次需要处说明，必要时简短回指。
- 结果章先说发现,再说一次口径;不要每个小节重新讲解怎么计算差值、百分比。
- 不造生硬复合词(如 "gigafloating-point"):首次出现写 "billions of floating-point operations (GFLOPs)",其后用缩写。
- 不罗列没做的实验、没测的指标或反复自我辩护。影响论断成立的条件、不确定性和重要反例必须保留。
  综合局限按审定大纲放在专门的局限、讨论或结论位置，不在各章重复。
- 正面、自信地陈述贡献和结果;但不夸大——数据没显示的性质(显著性、泛化、速度)不提,而不是先提再否认。
