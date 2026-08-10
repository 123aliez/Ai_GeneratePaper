# Paper Agent — 实验数据驱动的多 Agent 论文写作框架

从**你的创新点**、**论文结构**和**实验结果**自动产出实验型研究论文的框架。
三个异构大模型 Agent 在文件系统上协作,逐章完成"证据挖掘 → 规划 → 分段起草 →
评审 → 收敛修订 → 定稿 → 跨章交接"的迭代,全程被确定性的 Python 代码编排和把关。

> 给人类看懂这个项目是干嘛的、为什么这么设计。**怎么跑**见 `启动说明.md`;
> **每步干什么**见 `工作流总览.md`;**每份产物是什么**见 `文档说明.md`;
> **代码文件各干什么**见 `代码结构.md`。

---

## 它解决什么问题

让 LLM 直接"写一篇实验论文",最大的坑是**幻觉**:它会把方法章写成结果复述、把没跑过的
实验编成漂亮数字、把结论说得比证据强。这个框架的核心目标不是"写得流畅",而是
**保证贡献和数字都来自你提供的资料,绝不替你编**。

为此它把三件事彻底分开,每件都有**唯一来源**,框架不替你补任何一项:

| 维度 | 唯一来源 | 缺了会怎样 |
|---|---|---|
| **贡献是什么** | `idea.md`(你写的一句话贡献、核心洞察、方法设计) | idea 类章节**拒绝起草** |
| **数字是什么** | `data/`(你的实验结果 CSV/JSON/日志/图) | data 类章节**拒绝起草** |
| **结构是什么** | `outline_draft.md`(你写的分几章、每章什么类型) | 跑不起来(`--init` 没输入) |

---

## 三个 Agent 各干什么

| Agent | 角色 | 典型模型 | 职责 |
|---|---|---|---|
| **规划者** Manager | 规划 | Claude Opus / GPT-5.x | 读 idea.md 补要点、备料、起草前的段规划、统一定义术语 |
| **起草者** Draft | 写作 | Claude Opus | 挖证据、写正文、改稿 |
| **评审者** Review | 审稿 | GPT-5.x / Claude Opus | 评审、验改、定稿、跨章交接 |

> 这三个是**平级**的执行者,彼此**只通过读写文件协作,不直接对话**。
> 推进流程、门禁、断点续跑、重试全是确定性的 Python 代码,不是某个 Agent 在编排别的 Agent。
> 三个 Agent 的 provider/model/key 各自独立(OpenAI 或 Claude 任意混搭),见 `.env.example`。

---

## 为什么这样设计

- **贡献 ≠ 数字**。论文的价值是创新点和原理,实验数据只是支撑证据。所以方法/引言/摘要章
  从 `idea.md` 取材,只有结果/实验章才读 `data/`——早期版本让每章都读结果库、每章都过
  严门禁,方法章因此被写成结果复述,这是架构级偏差,已按类型分流修正。
- **`idea.md` 是全局一份**,每个阶段提示词第一行直接指向它,Agent 读全文。不复制进任何
  聚合文件——转述 upstream 正是论文创新点前后漂移的根源。
- **三道防幻觉门禁**:数字对账(正文每个数对回 `data/`)、引用闭合(只从你给的文献补
  `\cite`,匹配不到交人工、绝不编造)、贡献核查(idea 缺失或未填模板就硬停)。
- **收敛循环**:冻结首轮评审的"必须修"清单当验收单,起草者改一轮、评审者验一轮,
  直到清空或跑满上限,不空转。
- **跨章交接**:每章定稿后自动把术语和结论写进共享状态文件,下一章沿用——避免每章
  重新定义一遍符号。

---

## 最小能跑

> 完整步骤见 `启动说明.md`。这里只给骨架。

```bash
pip install -r requirements.txt
cp .env.example .env            # 填三组 provider/model/key(OpenAI 或 Claude 各自独立)
cp idea.example.md idea.md      # 填你的贡献/核心洞察/方法设计(最重要)
cp outline.example.md outline_draft.md  # 填章节骨架(表格)

python run.py --expand          # 规划者读 idea.md 补每章要点
                                #   → 生成 outline.md(英文版) + outline.zh.md(中文版)
                                #   普通章拆 ### 小节;短章(abstract/conclusion/...)要点挂 ## 下
python run.py --init            # 检测到中文版更新 → 自动翻译成英文覆盖 outline.md
                                #   → 各章工作区 + 跨章状态(+ data-index.md)
python run.py --retrieve        # 可选:按章检索文献候选 → references/candidates.md(不进 bib)
python run.py --all --progress  # 按 outline 顺序跑全部,失败即停
```

**双协议模型**:OpenAI 走 smolagents 原生 `OpenAIModel`,Claude 走原生 `/v1/messages`(不经 LiteLLM)。
三组 Agent 各自配 `*_PROVIDER`(openai/anthropic),可任意混搭。`*_REASONING_LEVEL` 统一 6 档语义,
按模型能力映射原生参数,不支持会显式报错。

**中英双轨**:`outline_draft.md` 是骨架,`--expand` 一次生成英文版 `outline.md`(Agent 读)
和中文版 `outline.zh.md`(你看/改)。你只改中文版,`--init` 时框架自动把中文翻译成英文
覆盖 `outline.md`,再生成工作区——**你永远只维护中文,Agent 永远吃英文**。

**短章**(`abstract`/`conclusion`/`limitations`/`discussion`,200-600 词)**不拆 `###` 小节**,
要点直接写在 `##` 章标题下,单段起草;字数标在 `##` 行(如 `## 1. Abstract (~200 words)`)。
普通章字数粒度仍是 `###` 小节。

写方法章**不必等实验跑完**:`type: method` 的门禁是 advisory,`data/` 为空也能起草,
数字标 UNVERIFIED;只有 `type: results` 这类章节才会在无数据时被拦。

`idea.md`、`outline_draft.md`、`outline.md`、`data/`、`.env` 都不进 git
(`.gitignore` 挡掉),模板和契约文档进。

---

## 延伸阅读

| 文档 | 内容 |
|---|---|
| `启动说明.md` | 从零跑通的操作步骤 + 常见问题 |
| `工作流总览.md` | 每一步读什么、产出什么、谁执行 |
| `文档说明.md` | 流水线里每份产物 md 是什么、谁写谁读 |
| `代码结构.md` | 16 个代码文件各干什么、互相怎么调(读代码 / 改代码 / 定位 bug) |
| `TODO.md` | 待办与已知差距 |
| `data/README.md` | 实验结果的数据契约 |
