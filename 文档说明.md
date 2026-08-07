# paper_agent — 文件清单与交付状态

实验数据驱动的多 Agent 论文写作框架。从 `survey/` 的综述引擎复制并改造而来:内容源从"文献笔记库"换成"**创新点文档 + 实验结果库**",并补齐了报告里讨论的检索、门禁、收敛、防幻觉机制。

**两个内容源,按章节类型分流**:论文的贡献是创新点,实验数据只是支撑。`brief.md` 顶部一行 `type: method` 决定这章从 `idea.md` 还是 `data/` 取材,以及数字门禁是 blocking / advisory / off。

**状态图例**

| 标记 | 含义 |
|---|---|
| ✅ 已建并测 | 代码写好,通过 import + 自测(离线,不调 API) |
| 🔶 已建待接线 | 模块可用、可单测,但尚未接进主流水线的 Agent 工具层 |
| 🟡 部分实现 | 机制到位但非完全体(下方注明差在哪) |
| 📥 需你提供 | 框架留了位,内容要你填 |
| ⏳ 需真实数据/密钥才能验 | 代码就绪,端到端跑通依赖你的输入 |

---

## 一、我建好的文件(共 27 个)

### 核心引擎(从 survey 迁移 + 改造)

| 文件 | 行数 | 状态 | 说明 |
|---|---|---|---|
| `config.py` | 96 | ✅ | 独立项目配置。`PAPER_MODE` 开关(experiment/survey)、`IDEA_PATH` 创新点文档、项目自包含路径、`RETRIEVAL_*` 检索 LLM、`AUTO_CITE_WEB`、`MAX_REVISION_ROUNDS` |
| `run.py` | 100 | ✅ | CLI 入口。`--progress`/`--direct`/`--list`,已适配新项目路径 |
| `agents/agents.py` | 63 | ✅ | 三 Agent 装配(Draft/Review/Manager) |
| `agents/prompts.py` | 387 | ✅ | 提示词。含 #6 跨段一致性、#7 JSON 评审+VERIFY 模式、实验模式防幻觉约束(借鉴 AI-Scientist per_section_tips) |
| `agents/orchestrator.py` | 941 | 🟡 | 六步流水线 + 收敛循环 + 类型路由接线。已接:类型路由、证据挖掘前置、内容源上下文、分级 number gate。见下方"部分实现" |
| `agents/tools.py` | 118 | ✅ | read/write/list/search_references 四工具 |
| `generate_reference_index.py` | — | ✅ | 文献索引生成器(survey 沿用) |

### 新增机制模块(报告里讨论的优化)

| 文件 | 行数 | 状态 | 对应任务 |
|---|---|---|---|
| `agents/chapter_type.py` | 306 | ✅ | **章节类型路由**:解析 `brief.md` 的 `type:` → (取材源 family, 门禁级别 gate)。13 种类型 + 中英别名 + 小节级覆盖 + 文件夹名兜底 |
| `agents/content_source.py` | 218 | ✅ | #13 内容源抽象:按 family 排序证据(idea 章 `idea.md` 优先、data 章结果表优先),`idea.md` 缺失时明确声明而非编造 |
| `agents/evidence_mining.py` | 128 | ✅ | #9 起草前多视角提问(STORM)。**视角随类型切换**:idea 章问机制/新颖性,data 章问显著性/baseline |
| `agents/number_gate.py` | 460 | ✅ | #13 数字一致性门禁:正文数字比对结果库,邻近度绑定+量纲缩放容错 |
| `agents/citation_check.py` | 383 | ✅ | #11 引用闭合校验 + LaTeX 日志解析(借鉴 AI-Scientist generate_latex) |
| `agents/citation_supplement.py` | 279 | ✅ | #12 缺引用自动补,带 URL 验证 + "禁编造"降级(选不到就标 needs_human) |
| `agents/retrieval.py` | 264 | 🔶 | #10 两层检索:笔记库(tier-1)+ 网页 LLM(tier-2)。已单测,**未接成 Agent 工具**(见下) |

### 测试

| 文件 | 状态 | 说明 |
|---|---|---|
| `tests/test_routing.py` | ✅ | 类型路由**单元**测试,43 项检查:type 解析/别名/兜底/小节联合/分段路由/视角切换/审稿判据/context pack 排序/缺 idea 声明。不调 API |
| `tests/test_pipeline_routing.py` | ✅ | 类型路由**流水线**测试,25 项检查:用记录型假 Agent 驱动完整 `run_4stage_with_progress`,验证两处 pre-flight 门禁真的在调模型前拦住、三个起草段各自路由正确。不调 API |

### 编译链

| 文件 | 行数 | 状态 | 说明 |
|---|---|---|---|
| `latex/build.py` | 92 | ✅ | 编译前跑引用闭合门禁 → 编译 → 带日志再解析 undefined。门禁不过直接非零退出 |
| `latex/main.tex` | — | 📥 | 论文 LaTeX 模板骨架,正文由 Agent 产出或你填 |

### 脚手架 / 文档 / 模板

| 文件 | 状态 | 说明 |
|---|---|---|
| `README.md` | ✅ | 项目总览 + 快速开始 |
| `MANIFEST.md` | ✅ | 本文件 |
| `.gitignore` | ✅ | 挡住 .env / 构建产物 / __pycache__ |
| `.env.example` | ✅ | 环境变量模板(四组模型 key + 检索 LLM) |
| `requirements.txt` | ✅ | 依赖(smolagents + dotenv;**未含 torch**,检索走你的 LLM) |
| `data/README.md` | ✅ | **数据契约**——定义你要放什么进 `data/` |
| `data/final_info.example.json` | ✅ | 结果文件示例(指标 JSON) |
| `data/results.example.csv` | ✅ | 结果文件示例(CSV) |
| `idea.example.md` | ✅ | **创新点文档模板**——8 节结构:一句话贡献 / 问题动机 / 核心洞察 / 方法设计(含公式符号) / 与前人 delta / 贡献清单 / 已知局限 / 预期结论 |
| `references/bibliography.md` | 📥 | 参考文献表模板,你填条目 |
| `workspace/_TEMPLATE/brief.md` | 📥 | 章节规格模板(含 `type:` 声明表),复制成你论文的 brief |

---

## 二、需要你提供的东西(框架跑起来的前提)

| # | 你要提供 | 放哪 | 为什么 |
|---|---|---|---|
| 1 | **创新点文档** ⭐ | 复制 `idea.example.md` → `idea.md`(项目根) | 📥 **最重要的一份**。Method/Intro/Related/Abstract 全靠它取材。没有它,idea 类章节**直接拒绝起草**(创新点只有你能给,框架不编) |
| 2 | **实验结果** | `data/` 下(CSV/JSON/log/图) | 📥⏳ Results/Experiments/Ablation 章的内容源,格式见 `data/README.md`。缺它只拦 data 类章节 |
| 3 | **API 密钥**(Draft/Review/Manager 三组 + 可选检索 LLM) | 复制 `.env.example` → `.env` 填入 | ⏳ 不填无法调模型 |
| 4 | **论文规格 brief**(顶部必须写 `type:`) | 复制 `workspace/_TEMPLATE/` → `workspace/你的论文名/brief.md` | 📥 定义章节结构和字数;`type:` 决定取材源和门禁级别 |
| 5 | **参考文献** | `references/bibliography.md` | 📥 引用闭合校验和自动补引用要对账它 |
| 6 |(可选)**LaTeX 正文/模板调整** | `latex/main.tex` | 📥 想编译 PDF 时 |

`idea.md` 和 `data/` 内容都被 `.gitignore` 挡掉(未发表的东西不进版本库),模板和契约文档进。

### `brief.md` 的 `type:` 写什么

| 写这个 | 主输入 | 数字门禁 | 中文别名 |
|---|---|---|---|
| `abstract` | idea.md | advisory | 摘要 |
| `intro` | idea.md | advisory | 引言 / 绪论 |
| `related` | idea.md + 文献 | off | 相关工作 |
| `background` | idea.md + 文献 | off | 背景 / 预备知识 |
| `method` | **idea.md** | advisory | 方法 / 模型 |
| `theory` | idea.md | advisory | 理论 |
| `experiments` | **data/** | blocking | 实验 / 实验设置 |
| `results` | **data/** | blocking | 结果 / 评估 |
| `analysis` | data/ | blocking | 分析 |
| `ablation` | data/ | blocking | 消融 / 消融实验 |
| `discussion` | 两者 | advisory | 讨论 |
| `limitations` | 两者 | advisory | 局限 / 不足 |
| `conclusion` | 两者 | advisory | 结论 / 总结 |

- 一份 brief 写整篇论文:顶部写主类型,小节下写 `- type: results` 覆盖该节。框架**按小节和按起草段分别路由**——Abstract+Intro 段用 idea.md,Results 段用 data/
- 写错的类型会退回 mixed/advisory 并在终端提示合法值,不会静默
- 不写 `type:` 会尝试从文件夹名推断(`05-results` → results),仍推不出就 mixed/advisory 并提示你补

---

## 三、部分实现 / 已知差距(诚实说明)

**架构级修正:章节类型路由(2026-08-07)**

早期版本让**每章都读结果库、每章都过 fail-closed 数字门禁**。这是架构级偏差,两个后果:
方法章被写成结果复述(创新点这个真正的主输入根本没进上下文),而且实验没跑完就没法先写方法。

现已按类型分流,四处联动:

| 环节 | 修正 |
|---|---|
| context pack | idea 章把 `idea.md` 排在最前并标成 PRIMARY,结果表降级成 SUPPORTING;data 章反过来 |
| 证据挖掘视角 | idea 章问「新颖性/机制/精确性/替代设计」,data 章问「显著性/baseline/可复现/过度声称」 |
| 起草提示词 | 按**每个起草段自己覆盖的小节**路由,不是整章一刀切 |
| 审稿判据 | idea 章明确告诉审稿"不要索要统计显著性、缺指标不算缺陷";data 章要求逐个数字对账 |
| 数字门禁 | blocking(data 章,无数据拒绝起草)/ advisory(idea 章,无数据只提示 UNVERIFIED)/ off(纯论述章不跑) |
| pre-flight 门禁 | **两个主输入都在调模型前检查**:idea 类缺 `idea.md`、data 类缺 `data/` → 直接中止。原先 data 类要等 Stage 0+1 跑完(4 次模型调用)才被 number gate 拦住,现在提前到起草前 |

**已完成的优化(codex 审查一轮后)**
- **Critical 全修**:C1(实验模式强制 progress)、C2(收敛严格验收,空清单不假通过)、C3(无效 review JSON 强制重跑)、C5(证据挖掘被 planner 消费)、C6(实验模式不注入 survey 索引)、C7(数字门禁 fail-closed)、C11(引用补全落地成 `\cite` 插入)
- **Important 修了 3 个核心**:I11(示例数据不当真)、I2(final 也过数字门禁)、I15(检索能搜 bibliography)
- **C5/C11 接线完成**:evidence-pack 只喂 planner、draft 按需读;review 标记 needs_citation、Python 插 `\cite`
- **文档**:`docs/工作流总览.md` 已更新(含上下文分层原则、引用补全、final 数字门禁)

**🟡 #8 增量补丁修改(orchestrator 收敛循环)**
现状:收敛循环里让 Draft "只改问题处、保留正确段落",是**提示词层面的引导**,不是真正的 SEARCH/REPLACE 补丁应用。真·补丁(Python 定位段落做无损替换、匹配失败兜底)还没做。影响:省 token 和"护住好段落"的效果不如报告设想的确定性版本。要不要升级成硬补丁,你定。

**🟡 #16 剩余 Important / #17 Minor(codex 报告)**
codex 审查发现的 Important 修了 3 个核心(I2/I11/I15),剩余 ~17 个和 8 个 Minor 标记**延后**——多数是健壮性打磨(I19 防 SSRF、I16 真 web 检索等),在当前"web 检索默认关"的前提下触发概率低,等真数据试跑暴露问题再针对性修,比现在无差别修完更有价值。

**⏳ 端到端未跑(等你数据)**
所有验证都是**离线的**:import 通过(14 模块)、确定性门禁(引用闭合/数字一致/自动补引/检索)自测通过、类型路由 43 项单元检查 + 25 项流水线检查通过、两处 pre-flight fail-closed 已验证不调模型、CLI 列表正常。但**没用真 API + 真数据跑过一整篇**。真实 LLM 能否稳定吐合法 JSON、VERIFY 判定准不准、证据挖掘质量如何——要等你拿数据+密钥试跑才知道。这正是你说的"最后拿实际数据测"。

---

## 四、你拿到数据后的启动步骤

```bash
cd paper_agent
pip install -r requirements.txt          # 装依赖
cp .env.example .env                      # 填四组模型 key(+ 可选检索 LLM)

cp idea.example.md idea.md                # ⭐ 写创新点/原理/方法设计(最重要)
# 把实验结果放进 data/(格式见 data/README.md)
# 把 references/bibliography.md 填上文献

cp -r workspace/_TEMPLATE workspace/my-paper   # 建你的论文工作区
# 编辑 workspace/my-paper/brief.md:顶部写 type:,再定义章节

python tests/test_routing.py              # 确认路由正常(离线,不花钱)
python tests/test_pipeline_routing.py     # 确认流水线接线正常(离线,不花钱)
python run.py --list                      # 确认能看到 my-paper
python run.py "my-paper" --progress       # 跑六步流水线
python latex/build.py --paper my-paper    # 编译 + 引用门禁
```

**跑起来先看第一行 `[Manager] route |`**——它会打印解析出的类型、取材源、门禁级别和各小节路由。
如果和你的预期不符,改 `brief.md` 的 `type:` 而不是改代码。
