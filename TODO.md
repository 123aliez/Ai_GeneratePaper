# TODO — 待处理清单

> ~~框架代码已建成并离线验证(五套测试全绿),但**从未用真 API + 真数据跑过一整篇**。~~
> **更新(2026-10-08)**:端到端已用真网关(gpt-6-luna)+ 编造的 SpecNet 草稿完整跑通
> 两章六阶段(见 PROJECT.md 进度记录);下面第 7~13、19 项的"从未验证"多数已有答案。
> 离线测试现为 12 套 175 项全绿(2026-10-13 新增 `test_draft_reliability.py`)。

---

## 一、卡住流程的(需要你提供,不提供就跑不动)

| # | 事项 | 位置 | 缺了会怎样 |
|---|---|---|---|
| 1 | **填 `idea.md`** | 根目录(已建骨架) | idea 类章节(Abstract/Intro/Related/Method)pre-flight 直接拒绝。当前是未填模板,实质内容 0 词——**未填的模板同样会被拦**,不会拿模板里的提问当论文的主张 |
| 2 | **写 `outline.md`** | 根目录(`cp outline.example.md outline.md`) | `--init` 没有输入,跑不起来 |
| 3 | **放实验结果** | `data/results/<run>/final_info.json` | data 类章节(Results/Experiments/Ablation)pre-flight 直接拒绝 |
| 4 | **填参考文献** | `references/bibliography.md` | 目前只有 1 行示例。引用补全无 key 可匹配,全部标 `needs_human` |
| 5 | **填参考文献清单** | `references/bibliography.md` | 各章 `input.md` 由 Stage 0a 时规划者从它生成;清单空则 input 内容空洞 |
| 6 | **填 API key** | `.env`(已存在但值为空) | 无法调模型。三组 Agent 各自独立配 |

第 1 项最关键:`idea.md` 的第 3 节(核心洞察)和第 4 节(方法设计)是框架**唯一不能替你补**的部分。

---

## 二、从未验证的(等真实跑通才知道)

| # | 未知项 | 为什么现在答不了 |
|---|---|---|
| 7 | 真 LLM 能否稳定吐合法 JSON | `review-v1.json` 的 schema 约束只在离线用假 Agent 测过。真模型在新增的类型判据 + 写作契约两段子句下会不会破坏 JSON 格式,未知 |
| 8 | VERIFY 判定准不准 | 收敛循环靠 Review 判断 MUST FIX 是否已解决。判松了会假通过,判严了会跑满 4 轮 |
| 9 | 证据挖掘质量 | idea 类视角(机制/新颖性)真模型答出来的东西有多少可用,未知 |
| 10 | 骨架检测阈值 40 词是否合适 | "剥掉提问块后不足 40 词判为未填写"是拍的数。你填完 `idea.md` 如果被误判,调 `content_source.py::idea_is_skeleton` |
| 11 | 审稿规范与提示词是否冲突 | `skills/review-rubric.md` 要求查数据-结论 mismatch,但提示词的类型判据告诉 Review "方法章不要索要统计显著性"。理论上提示词优先(它在任务里),**但没验证**。若方法章的 MUST FIX 里仍出现"缺显著性检验",根因在此 |
| 12 | **Stage 5 的 upsert 契约真模型能否遵守** | 它要求每条 bullet 以 `- [<章名>] ` 开头并替换同前缀旧条目。Python 会校验标记是否出现在 Key Claims 小节内,校验失败会拦住 `--all`——但真模型多久能一次写对,未知 |
| 13 | **写作契约的实际效果** | 写作契约已注入每个改写正文的阶段并有测试断言,但"真模型会不会照做"要看实稿:会不会重复定义前章符号、会不会写指向不存在章节的过渡 |

---

## 三、代码层已知缺口

| # | 缺口 | 影响 | 建议 |
|---|---|---|---|
| 14 | **增量补丁是提示词引导,非真 SEARCH/REPLACE** | 收敛循环让 Draft"只改问题处",靠提示词约束。省 token 和"护住好段落"的确定性不如硬补丁 | 等真跑几轮看它会不会重写无关段落,再决定要不要升级 |
| 15 | **章节字数标准未写进提示词** | `format_stage1_parts` 按比例给区间(`target*0.85` ~ `target*1.2+30`),没有"顶会各类型章节平均多少词"的参考 | 待办:调研顶会各章节典型字数,作为参考写进提示词 |
| 16 | **codex 审查剩余 Important / Minor** | 多数是健壮性打磨(防 SSRF、真 web 检索等)。在"web 检索默认关"前提下触发概率低 | 延后,等真数据暴露问题再针对性修 |
| 17 | **`PAPER_MODE=survey` 路径在本项目里是残留** | 从 survey 复制来的分支,本项目没有 `paper/00 Background` 那套目录。已加 `_assert_survey_mode` 守卫,非 survey 模式下调用会明确抛错而非静默读到不存在的文件 | 可以删,但守卫已足够;留着供对照 |
| 18 | **`latex/main.tex` 是骨架** | 编译 PDF 时才需要,正文由 Agent 产出或你填 | 到编译阶段再处理 |
| 19 | ~~端到端没跑过真模型~~ | 已跑通(2026-10-08,网关 gpt-6-luna,SpecNet 两章六阶段:RES 占位符 0 残留/收敛一轮过/指纹记账闭环/跨章状态齐备)。剩余未知:更多章节类型(intro/related/abstract)与真实 idea 的表现 | 后续用真课题跑一篇完整论文 |
| 20 | **指纹碰撞(备选)** | brief 指纹取 sha256 前 12 位。两章内容不同但指纹相同会让 brief 误判为"已生成"——实际只会改变章标题/type/小节/要点之一就会换指纹,碰撞概率极低 | 当前不修,等真出现再处理 |
| 21 | **agent 偶发"响应成功但不写文件"** | 网关/模型单次行为(2026-10-08 全链路测试 Stage 3 Round 1 出现一次,重跑即过)。`run_agent_stage` 兜底只覆盖异常路径,正常返回但产物缺失走下游 MISSING 硬停 | 已确认非代码缺陷;重跑即续。若高频出现,考虑在 run_agent_stage 加"产物缺失也重试"逻辑 |

---

## 四、已完成(累计)

- **outline 驱动的生成**:`outline.md` → `run.py --init` → 各章工作区 + 跨章状态;
  `--all` 按 outline 顺序跑全篇
- **写作契约**:整篇对齐(第几章/可跨章引用/复用前章符号),契约注入每一个改写正文的阶段
- **Stage 5 跨章交接自动化**:原先只打印"记得手动更新",现在自动 upsert + Python 校验,
  失败会拦住 `--all`
- **路由指纹**:类型路由写进 `brief.md` 首行的 outline 指纹;路由变了而旧产物还在 → 硬停
  (一个文件都不删)
- **brief 来源门禁**:非生成式 brief / brief 过期 / 缺跨章状态,全部在调模型前硬停
- **分段自适应**:段数 `min(小节数, 3)`,拼接按本次实际段数(不再硬编 3)
- **删掉 Alignment 章硬编特例**(原 #12)
- **`retrieval.py` 接成 `search_literature` 工具**(原 #14),三个 Agent 都能调;
  web 命中标为"线索,不可引"
- **`chapter_fingerprint` 改用 sha256**(原先用内置 `hash()`,受 `PYTHONHASHSEED` 影响,
  `--init` 与运行是两次进程,指纹永远对不上)
- **删掉逐章模式**:架构简化为单一整篇路径,章节文件夹一律由 outline.md 经 --init 生成
- **Model 层瞬时重试**(2026-08):`retry_policy` + `resilient_model`,网关抖动从"重启整个
  CodeAgent(分钟级)"收拢到 Model 层重发(秒级),真实高峰期 4 次自然抖动 100% 秒级自愈
- **多用户 Web 平台层**(2026-08,分支 `feat/web-platform`):`server/`(pipeline 子进程隔离 +
  db/dispatcher/worker/web + docker-compose),不动 agents/
- **证据硬化**(2026-10-08,分支 `feat/evidence-hardening`):RES 数字占位符范式(`result_ids.py`,
  起草只许引 `{{RES-xxx}}`、定稿程序替换、未知 ID 留洞不编值)+ 阶段指纹记账(`skills_registry.py`,
  提示词/skill/模型变更后旧产物硬停不静默复用)+ review JSON 单轮直调(`structured_call.py`)
- **inbox 散装材料前处理层**(2026-10-08,同分支):`--intake` 纯 Python 盘点,idea 文档逐字转正,
  data 对账同判据只盘点不搬运(材料归位一律作者手动,不做自动领养)
- **初稿可靠性 8 项**(2026-10-13,分支 `fix/draft-reliability`):结果库 seed 等身份列进 key、
  重复 key/读不了的文件拒绝生成 RES 目录;RES 编号退役永不复用;阶段指纹加入 idea.md 与数值
  结果库(改了 idea/数据再跑会硬停,`--all` 跳过前也复查);章节运行期 `write_file` 只能写本章
  目录;"生成完成"与"核验通过"分离(`verification.md`);review 判定只认布尔 true,到上限时
  漏报/损坏的核验项全部上报;Stage 4 只基于 draft-v2 定稿;本地 skill 由编排器注入。
  **升级影响**:指纹公式变了,旧工作区产物会被一次性判陈旧,需备份后删除重跑
- **端到端真网关验证**(2026-10-08):SpecNet 编造草稿两章六阶段全绿,RES 替换 0 残留、
  收敛循环一轮通过、指纹记账闭环、resume 三类跳过全部生效

---

## 五、建议的推进顺序

```
1. 填 idea.md(只填第 3、4 节也够起步;材料零散可先扔 inbox/ 跑 python run.py --intake 转正)
2. cp outline.example.md outline_draft.md,写好章节骨架
3. 配 .env 的三组 key
4. python run.py --expand        # 骨架 → 中英双份 outline
5. python run.py --init          # 生成各章工作区
6. python run.py --list          # 确认每章的类型对不对
7. 先跑纯 idea 类章节(不需要 data/):
   python run.py "04-method" --progress
8. 看 route / write-mode 两行输出对不对;看 evidence-pack.md 质量
9. 实验跑完后补 data/,再跑 results 章
10. 全篇:python run.py --all --progress
11. python latex/build.py
```

第 6 步是最省的验证路径——方法章不需要实验数据,可以在实验还没跑完时就验证整条流水线。

---

## 六、改了路由之后要做什么

**正确做法:删陈旧产物,而不是只 `--init --force`。** 类型路由(类型/小节类型)写进 `brief.md`
首行的 outline 指纹。改了 type 而旧产物还在,流水线会**硬停**并列出所有按旧路由生成的产物
(含 `brief.md`、`evidence-pack.md`、`draft-v1.md`、`review-v1.json`、`final.md` 等)。备份后
删掉这些文件再重跑,才能让它们按新路由重新生成。

**为什么不能只跑 `--init --force`**:那只会刷新 `brief.md` 的指纹,把"路由变了"的信号擦掉,
而下游的 evidence-pack / plan / review / final 仍是旧路由的,会被"存在即跳过"静默复用——
看起来跑通了,实际还是旧路由的内容。

改了 `outline.md` 的结构(章数/标题/顺序)后,`--init --force` 会刷新各章 brief——这跟"改 type"
不同,改结构不会动证据路由,下游产物继续有效。`input.md` 由 Stage 0 生成,不归 `--init` 管,
自然不动。
