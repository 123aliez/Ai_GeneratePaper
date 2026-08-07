# Paper Agent — 实验数据驱动的多 Agent 论文写作框架

从**你的创新点**(`idea.md`)加**实验结果**(`data/`)生成实验型论文的多 Agent 框架。三个异构 Agent(Draft = Claude Opus / Review = GPT-5.5 / Manager = GPT-5.5)在文件系统上协作,对每个章节执行"规划 → 分段起草 → 评审 → 收敛修订 → 定稿"的迭代。

这是 `survey/` 框架的实验型分支。核心区别:**内容源从文献笔记库换成"创新点文档 + 实验结果库"**,并加入了防幻觉门禁、数字一致性校验、引用闭合校验和证据挖掘。

---

## 两个输入,不是一个

论文的贡献是**创新点和原理**,实验数据只是支撑证据。所以框架有两个内容源,按章节类型分流:

| 章节类型 | 主输入 | 结果表的角色 | 数字门禁 |
|---|---|---|---|
| `method` `intro` `abstract` `theory` | **`idea.md`** | 辅助证据(最多引一个 headline 数) | advisory |
| `related` `background` | `idea.md` + 文献 | 不用 | off |
| `results` `experiments` `analysis` `ablation` | **`data/`** | 就是内容本身 | **blocking** |
| `discussion` `limitations` `conclusion` | 两者 | 支撑论点 | advisory |

在 `brief.md` 顶部写一行 `type: method` 声明。一份 brief 写整篇论文时,在小节下写 `- type: results` 覆盖该节,框架按小节分别路由。

**为什么这样分**:早期版本让每章都读结果库、每章都过 fail-closed 数字门禁——方法章因此被写成结果复述,而且实验没跑完就没法先写方法。这是架构级偏差,现已按类型分流修正。

- `idea.md` 缺失时,idea 类章节**拒绝起草**并提示你去写(创新点只有作者能给,框架不替你编)
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
paper_agent/
├── run.py                     # CLI 入口
├── config.py                  # 模型/路径/模式/常量配置
├── requirements.txt
├── .env.example               # 复制成 .env 填 key
├── idea.example.md            # ← 复制成 idea.md:你的创新点/原理/方法设计
├── agents/
│   ├── agents.py              # Agent 组装
│   ├── prompts.py             # 三 Agent 指令 + 实验模式/防幻觉常量
│   ├── orchestrator.py        # 六步流水线 + 收敛循环 + 段间上下文
│   ├── tools.py               # read/write/list/search 工具
│   ├── chapter_type.py        # 章节类型路由(brief.md 的 type: → 取材源 + 门禁级别)
│   ├── content_source.py      # 内容源抽象(idea.md ↔ data/ ↔ survey笔记)
│   ├── evidence_mining.py     # 起草前多视角证据挖掘(STORM),按类型换视角
│   ├── citation_check.py      # 编译前引用闭合校验 + 日志解析
│   ├── number_gate.py         # 正文数字对账结果库(防幻觉)
│   ├── retrieval.py           # 两层检索:本地笔记/bib + 网页 LLM
│   └── citation_supplement.py # 缺引用自动补(带 URL 验证)
├── data/                      # ← 你提供:实验结果(见 data/README.md)
├── tests/test_routing.py      # 章节类型路由的离线测试(43 项,不调 API)
├── references/                # 参考文献 bib + 阅读笔记
├── skills/                    # 写作/图表/公式/审稿规范(Agent 按需读取)
│   ├── writing-style.md
│   ├── figure-table-placeholder.md
│   ├── math-formula.md
│   ├── review-rubric.md
│   └── experiment-writing.md
├── workspace/                 # 章节工作区(草稿/评审/定稿产物)
│   └── _TEMPLATE/brief.md     # 章节规格模板
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

# 4. 放实验数据(见 data/README.md 的格式契约)
#    data/<experiment>/final_info.json, results.csv, logs, plots/

# 5. 写章节规格(照 workspace/_TEMPLATE/brief.md,顶部必须声明 type:)
#    workspace/<paper>/brief.md + input.md

# 6. 跑一个章节
python run.py "<paper>" --progress
```

写方法章不必等实验跑完:`type: method` 的门禁是 advisory,`data/` 为空也能起草,
数字会标 UNVERIFIED。反过来,`type: results` 在 `data/` 为空时会在**调模型之前**拒绝起草。

```bash
# 改完 brief/idea 后先跑离线测试确认路由,不花 token
python tests/test_routing.py           # 43 项:type 解析、别名、分段路由、判据切换
python tests/test_pipeline_routing.py  # 25 项:假 Agent 驱动完整流水线,验证门禁接线
```

---

## 六步流水线(继承自 survey,已增强)

```
    Python  → 类型路由                               读 brief.md 的 type: → 取材源 + 门禁级别
    Python  → context-pack.md                       按类型排序证据(idea 优先 / data 优先)
0   Draft   → evidence-pack.md                      多视角挖证据,视角随类型切换
1a  Manager → draft-v1.plan.md + Notation Table     规划 + 术语/符号表冻结
1b  Draft ×3→ draft-v1.part-{1,2,3}.md              分段起草,每段按自己覆盖的小节类型路由
    Python  → draft-v1.md                           拼接
    Python  → number-check.md                       数字门禁(blocking/advisory/off 按类型)
2   Review  → review-v1.md + review-v1.json         评审(判据随类型)+ 结构化打分 + needs_citation
    Python  → citation-insertions.md                C11 引用补全(插 \cite 交人工清单)
3   收敛循环 → draft-v2.md                           冻结首轮MUST FIX,≤4轮修到清
4   Review  → final.md + final.zh.md + decision.md  定稿
    Python  → final 数字门禁 + citation_check        定稿复核 + 引用闭合
```

**类型路由贯穿全流程**:证据挖掘的提问视角、起草的主输入、审稿的判据、数字门禁的严格度,
四处都按章节类型切换。方法章的审稿不会要求补统计显著性,结果章的审稿不会要求补方法阐述。

**收敛循环**是本框架相对 STORM / AI-Scientist 的增强:冻结首轮 MUST FIX 当验收单,循环修订直到全部解决或到 4 轮上限(未清项升级到 todo/decision,不空转)。

---

## 你需要提供什么

见根目录 `MANIFEST.md` 的"需你提供"清单。最核心的四样:
1. **`idea.md`** — 你的创新点/原理/方法设计(模板 `idea.example.md`)。**这是最重要的一份**,idea 类章节全靠它
2. `.env` 里的 API key
3. `data/` 里的实验结果(格式见 `data/README.md`)
4. `workspace/<paper>/brief.md` 章节规格(顶部声明 `type:`)+ `input.md` 素材

`idea.md` 和 `data/` 都不进 git(`.gitignore` 挡掉),模板和契约文档进。
