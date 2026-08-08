# Paper Agent — 实验数据驱动的多 Agent 论文写作框架

从**你的创新点**(`idea.md`)、**论文结构**(`outline.md`)和**实验结果**(`data/`)生成实验型论文的多 Agent 框架。三个异构 Agent(Draft = Claude Opus / Review = GPT-5.5 / Manager = GPT-5.5)在文件系统上协作,对每个章节执行"证据挖掘 → 规划 → 分段起草 → 评审 → 收敛修订 → 定稿 → 跨章交接"的迭代。

这是 `survey/` 框架的实验型分支。核心区别:**内容源从文献笔记库换成"创新点文档 + 实验结果库"**,并加入了防幻觉门禁、数字一致性校验、引用闭合校验、证据挖掘和 outline 驱动的分章生成。

---

## 三份输入,职责分开

| 文件 | 管什么 | 缺了会怎样 |
|---|---|---|
| `idea.md` | **贡献是什么** — 一句话贡献、核心洞察、方法设计 | idea 类章节拒绝起草 |
| `outline.md` | **结构是什么** — 有哪几章、每章哪几节、各节多少字 | `--init` 没有输入,跑不起来 |
| `data/` | **数字是什么** — 实验结果(CSV/JSON/日志/图) | data 类章节拒绝起草 |

各自都是唯一事实来源。框架不替你编任何一项——缺了就明确报错或标记,不"合理地"补一个。

`brief.md` 不是第四份输入,它是 `outline.md` 的生成产物(每章一份)。

---

## 写一整篇论文

```bash
python run.py --init                 # outline.md → 各章工作区 + 跨章状态
python run.py --all --progress       # 按 outline 顺序跑全部,失败即停
python run.py 04-method --progress   # 也可以只跑其中一章(必须在 outline 里)
```

每章都是整篇里的第 N 章,知道前后是什么;符号沿用前章的定义(跨章状态由每章跑完后的
Stage 5 自动追加);开头接上文、结尾引下文;属于邻章的内容必须留给邻章。

`python run.py --list` 列出各章状态。`--all` 要求 workspace 与 outline 完全一致——
缺章(还没 `--init`)或多出磁盘残留(改标题后改名遗留)都会拒绝开始。

---

## 章节类型路由

论文的贡献是**创新点和原理**,实验数据只是支撑证据。所以有两个内容源,按章节类型分流:

| 章节类型 | 主输入 | 结果表的角色 | 数字门禁 |
|---|---|---|---|
| `method` `intro` `abstract` `theory` | **`idea.md`** | 辅助证据(最多引一个 headline 数) | advisory |
| `related` `background` | `idea.md` + 文献 | 不用 | off |
| `results` `experiments` `analysis` `ablation` | **`data/`** | 就是内容本身 | **blocking** |
| `discussion` `limitations` `conclusion` | 两者 | 支撑论点 | advisory |

在 `outline.md` 里给每章写一行 `type: method`(会写进生成的 brief)。小节下写
`- type: results` 可以覆盖该节,框架按小节和按起草段分别路由。

**为什么这样分**:早期版本让每章都读结果库、每章都过 fail-closed 数字门禁——方法章因此
被写成结果复述,而且实验没跑完就没法先写方法。这是架构级偏差,现已按类型分流修正。

- `idea.md` 缺失**或还是未填的模板**时,idea 类章节**拒绝起草**并提示你去写
  (贡献只有作者能给,框架不替你编)
- `data/` 缺失时,只有 data 类章节会被拦;方法章照常写,数字标 UNVERIFIED

---

## 与 survey 框架的关系

| | survey/ | paper_agent/(本项目) |
|---|---|---|
| 内容源 | 文献 `笔记.md` + `index.md` | `idea.md` 创新点 + `data/` 实验结果 |
| 事实核验 | 对回 arXiv 原文 | 数字对账原始结果(number_gate) |
| 证据挖掘 | 对笔记库多视角提问 | 按章节类型分流提问:idea 章问机制/新颖性,data 章问显著性/baseline |
| 防幻觉 | 证据分级 | per-section 硬约束 + 缺创新点/缺数据均 fail-closed |

---

## 目录结构

```
Ai_GeneratePaper/
├── run.py                     # CLI 入口(--init / --all / 单章 / --list)
├── config.py                  # 模型/路径/模式/常量配置
├── requirements.txt
├── .env.example               # 复制成 .env 填 key
├── idea.example.md            # ← 复制成 idea.md:你的贡献/原理/方法设计
├── outline.example.md         # ← 复制成 outline.md:整篇章节结构
├── agents/
│   ├── agents.py              # Agent 组装(三 Agent,五个共享工具)
│   ├── prompts.py             # 三份系统指令(跨章上下文按写作模式条件读取)
│   ├── orchestrator.py        # 六阶段流水线 + 收敛循环 + 路由门禁
│   ├── outline.py             # outline 解析 + 章节工作区生成 + 跨章状态
│   ├── tools.py               # read/write/list/search_references/search_literature
│   ├── chapter_type.py        # 章节类型路由(type: → 取材源 + 门禁级别)
│   ├── content_source.py      # 内容源抽象(idea.md ↔ data/ ↔ survey 笔记)
│   ├── evidence_mining.py     # 起草前多视角证据挖掘(STORM),按类型换视角
│   ├── citation_check.py      # 编译前引用闭合校验 + 日志解析
│   ├── number_gate.py         # 正文数字对账结果库(防幻觉)
│   ├── retrieval.py           # 两层检索:本地 bib(可引)+ 网页 LLM(仅线索)
│   └── citation_supplement.py # 缺引用自动补(带 URL 验证)
├── data/                      # ← 你提供:实验结果(见 data/README.md)
├── tests/                     # 离线测试,不调 API
│   ├── test_routing.py            # 类型路由单元
│   ├── test_outline.py            # outline 解析、brief 往返、写作契约
│   ├── test_pipeline_routing.py   # 流水线级接线(假 Agent 驱动)
│   ├── test_optimizations.py      # 分段/指纹/工具链
│   └── test_expand.py             # --expand 越权校验
├── references/                # 参考文献 bib + 阅读笔记
├── skills/                    # 写作/图表/公式/审稿规范(Agent 按需读取)
│   ├── writing-style.md
│   ├── figure-table-placeholder.md
│   ├── math-formula.md
│   ├── review-rubric.md
│   └── experiment-writing.md
├── workspace/                 # 章节工作区
│   ├── cross-chapter-state.md # (--init 生成 + Stage 5 追加)跨章术语与结论载体
│   └── <NN-章名>/             # (--init 生成 brief.md;Stage 0 生成 input.md + 各产物)
└── latex/                     # LaTeX 编译(build.py 带引用闭合校验)
```

---

## 快速开始

```bash
# 1. 装依赖
pip install -r requirements.txt

# 2. 配 key
cp .env.example .env   # 填 DRAFT/REVIEW/MANAGER + 可选 RETRIEVAL

# 3. 写创新点文档(最重要的一步,照 idea.example.md)
cp idea.example.md idea.md   # 填:一句话贡献 / 核心洞察 / 方法设计 / 与前人的 delta

# 4. 写论文结构(照 outline.example.md)
cp outline.example.md outline.md   # `##` 是章(带 type:),`###` 是小节(带 ~N words)

# 5. 放实验数据(见 data/README.md 的格式契约)
#    data/<experiment>/final_info.json, results.csv, logs, plots/

# 6. 生成各章工作区(顺带让 Manager 读 data/ 生成 data-index.md)
python run.py --init          # → workspace/<NN-章名>/brief.md + 跨章状态 + data/data-index.md
#    input.md 由 Stage 0 时 Manager 从你的 bibliography.md 自动生成

# 7. 确认路由,然后跑
python run.py --list          # 每章的路由 + 类型 + 状态
python run.py --all --progress
```

写方法章不必等实验跑完:`type: method` 的门禁是 advisory,`data/` 为空也能起草,
数字会标 UNVERIFIED。反过来,`type: results` 在 `data/` 为空时会在**调模型之前**拒绝起草。

```bash
# 改完 outline/brief/idea 后先跑这五套离线测试确认接线,不花 token
python tests/test_routing.py           # 类型解析、别名、分段路由、判据切换
python tests/test_outline.py           # outline 解析、brief 往返、写作契约
python tests/test_pipeline_routing.py  # 假 Agent 驱动完整流水线,验证门禁接线
python tests/test_optimizations.py     # 分段自适应、指纹、工具链
python tests/test_expand.py            # --expand 的越权改结构校验
```

---

## 流水线

```
    Python  → 路由解析                              章节位置 + brief 门禁 + type: → family/gate
0a  Manager → input.md                          从 bibliography 挑选本章文献素材(--init 不再生成 input.md)
0b  Draft   → evidence-pack.md                      多视角挖证据,视角随类型切换(idea.md 全文 + data-index.md)
1a  Manager → draft-v1.plan.md + Notation Table     规划 + 术语/符号表冻结
1b~ Draft ×N→ draft-v1.part-N.md                    分段起草(段数 = min(小节数,3)),每段按自己的小节类型路由
    Python  → draft-v1.md                           按本次实际段数拼接
    Python  → number-check.md                       数字门禁(blocking/advisory/off 按类型,直读 data/)
2   Review  → review-v1.md + review-v1.json         评审(判据随类型+随模式)+ 打分 + needs_citation
    Python  → citation-insertions.md                引用补全(插 \cite,选不到的交人工)
3   收敛循环 → draft-v2.md                           冻结首轮 MUST FIX,≤4 轮修到清
4   Review  → final.md + final.zh.md + decision.md  定稿
    Python  → number-check.md(覆盖)                定稿复核数字门禁,两次只留一个文件
5   Review  → cross-chapter-state.md                跨章交接 + Python 校验
```

> `idea.md` 是每个阶段提示词第一行直接指向的全局文件,Agent 用 read_file 读全文;
> 不再有任何聚合资料文件。data 类章节写数字时读 `--init` 生成的 `data/data-index.md` 导航。

**编排由 Python 做,不由 Manager 做。** Manager 只承担 Stage 0a 备料 + Stage 1a 的规划;
阶段推进、门禁、断点续跑、重试全是确定性代码。

**两个维度贯穿全流程**:
- **类型路由**(取材)— 证据挖掘视角、起草主输入、审稿判据、数字门禁严格度
- **写作契约**(结构)— 起草/审稿按整篇对齐、跨章状态读写

写作契约注入**每一个**会改写正文的阶段(1a / 1b~1c / Stage 3 每一轮 / Stage 4)。

**收敛循环**是本框架相对 STORM / AI-Scientist 的增强:冻结首轮 MUST FIX 当验收单,
循环修订直到全部解决或到 4 轮上限(未清项升级到 todo/decision,不空转)。

**断点续跑**:每个产物"存在即跳过"。所以证据路由(类型/小节类型)写进 `brief.md`
首行的 outline 指纹;路由变了(改了 type 没重新 --init)而旧产物还在,流水线**硬停**——
一个文件都不删,但也绝不用旧路由的产物继续。

---

## 你需要提供什么

完整清单见 `文档说明.md`。最核心的五样:

1. **`idea.md`** — 你的贡献/原理/方法设计(模板 `idea.example.md`)。**最重要的一份**
2. **`outline.md`** — 整篇章节结构(模板 `outline.example.md`)
3. `.env` 里的 API key
4. `data/` 里的实验结果(格式见 `data/README.md`;`--init` 会据此生成 `data/data-index.md`)
5. `references/bibliography.md` — 你的参考文献清单(Stage 0 据此生成各章 `input.md`)

> `input.md` 不再由你手写,也不由 `--init` 生成:跑某章的 Stage 0 时 Manager 会从
> 你的 `bibliography.md` 自动组织它。

`idea.md`、`outline.md`、`data/` 都不进 git(`.gitignore` 挡掉),模板和契约文档进。

---

## 延伸阅读

| 文档 | 内容 |
|---|---|
| `工作流总览.md` | 多 Agent 工作流全文:每阶段读什么、产出什么、谁执行 |
| `文档说明.md` | 文件清单、交付状态、已修正的架构偏差、已知差距 |
| `启动说明.md` | 逐步启动指引 |
| `data/README.md` | 实验结果的数据契约 |
| `TODO.md` | 待办与已知差距 |
