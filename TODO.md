# TODO — paper_agent 待处理清单

> 状态截至 2026-08-07。框架代码已建成并离线验证(80 项路由测试 + 6 模块自测全过),
> 但**从未用真 API + 真数据跑过一整篇**。下面按"卡住流程 / 没验证 / 代码缺口"三类排。

---

## 一、卡住流程的(需要你提供,不提供就跑不动)

| # | 事项 | 位置 | 缺了会怎样 |
|---|---|---|---|
| 1 | **填 `idea.md`** | 根目录(已建骨架) | idea 类章节(Abstract/Intro/Related/Method)pre-flight 直接拒绝。当前是未填模板,实质内容 0 词 |
| 2 | **放实验结果** | `data/results/<run>/final_info.json` | data 类章节(Results/Experiments/Ablation)pre-flight 直接拒绝 |
| 3 | **填参考文献** | `references/bibliography.md` | 目前只有 1 行示例。引用补全无 key 可匹配,全部标 `needs_human` |
| 4 | **写论文 brief** | `workspace/<论文名>/brief.md` | 没有就没有工作区。必须在顶部声明 `type:` |
| 5 | **填 API key** | `.env`(已存在但值为空) | 无法调模型。三组 Agent 各自独立配 |

第 1 项最关键:`idea.md` 的第 3 节(核心洞察)和第 4 节(方法设计)是框架**唯一不能替你补**的部分。

---

## 二、从未验证的(等真实跑通才知道)

| # | 未知项 | 为什么现在答不了 |
|---|---|---|
| 6 | 真 LLM 能否稳定吐合法 JSON | `review-v1.json` 的 schema 约束只在离线用假 Agent 测过。真模型在新增的类型判据子句下会不会破坏 JSON 格式,未知 |
| 7 | VERIFY 判定准不准 | 收敛循环靠 Review 判断 MUST FIX 是否已解决。判松了会假通过,判严了会跑满 4 轮 |
| 8 | 证据挖掘质量 | idea 类视角(机制/新颖性)是今天新加的,真模型答出来的东西有多少可用,未知 |
| 9 | 骨架检测阈值 40 词是否合适 | 当前"剥掉提问块后不足 40 词判为未填写"是拍的数。你填完 `idea.md` 如果被误判,调 `content_source.py::idea_is_skeleton` |
| 10 | 审稿规范与提示词是否冲突 | `skills/review-rubric.md` 要求查数据-结论 mismatch,但提示词的类型判据告诉 Review "方法章不要索要统计显著性"。理论上提示词优先(它在任务里),**但没验证**。若方法章的 MUST FIX 里仍出现"缺显著性检验",根因在此 |

---

## 三、代码层已知缺口

| # | 缺口 | 影响 | 建议 |
|---|---|---|---|
| 11 | **增量补丁是提示词引导,非真 SEARCH/REPLACE** | 收敛循环让 Draft"只改问题处",靠提示词约束。省 token 和"护住好段落"的确定性不如硬补丁 | 等真跑几轮看它会不会重写无关段落,再决定要不要升级 |
| 12 | **`build_stage1_parts` 里有 Alignment 章硬编码特例** | 从 survey 引擎继承来的,对实验论文无意义。当前会在含 "rlvr" + "constitutional" 小节时触发异常分组 | 建议直接删掉那个分支 |
| 13 | **codex 上轮审查剩余 Important(~17) / Minor(8)** | 多数是健壮性打磨(防 SSRF、真 web 检索等)。在"web 检索默认关"前提下触发概率低 | 延后,等真数据暴露问题再针对性修 |
| 14 | **`retrieval.py` 未接成 Agent 工具** | 两层检索已单测通过,但 Agent 拿不到这个工具,只能用 `search_references` | 需要时再接线 |
| 15 | **`PAPER_MODE=survey` 路径在本项目里是残留** | 从 survey 复制来的分支,本项目没有 `paper/00 Background` 那套目录,走这条路会读到不存在的文件 | 要么删,要么明确标注"仅供对照,不要用" |
| 16 | **`latex/main.tex` 是骨架** | 编译 PDF 时才需要,正文由 Agent 产出或你填 | 到编译阶段再处理 |
| 17 | **整篇 brief 没有真流水线端到端测试** | 六小节整篇论文的分段路由只用"提示词捕获"验过(假 Agent),没跑过真模型 | 属于第 6 项的一部分 |

---

## 四、建议的推进顺序

```
1. 填 idea.md(只填第 3、4 节也够起步)
2. 配 .env 的三组 key
3. 先跑纯 idea 类章节(type: method),不需要 data/
   python run.py "<论文名>" --progress
4. 看第一行 route 输出对不对;看 evidence-pack.md 质量
5. 实验跑完后补 data/,再跑 results 章
6. 全篇跑通后 python latex/build.py --paper <论文名>
```

第 3 步是最省的验证路径——方法章不需要实验数据,可以在实验还没跑完时就验证整条流水线。

---

## 五、改了 brief.md 的 `type:` 之后

`context-pack.md` 是按类型生成的,但断点续跑会跳过已存在的产物。**改完 `type:` 要删掉该章节的 `context-pack.md`**,否则读到的还是旧路由的包。
