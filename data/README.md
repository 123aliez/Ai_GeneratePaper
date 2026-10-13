# data/ — 你提供的实验数据(框架的输入契约)

这个目录是**你要填的**。框架从这里读实验结果来写论文,**绝不编造数字**。
正文出现的每个数值都会被 number gate 对回这里的原始数据校验。

> **只有 data 类章节才读这里。** `type: results` / `experiments` / `analysis` /
> `ablation` 的章节以本目录为主输入,没数据会在调模型前直接拒绝起草。
> `type: method` / `intro` 这类 idea 章节的主输入是根目录的 `idea.md`,
> `data/` 为空也能起草——所以**实验没跑完可以先写方法章**。

## 目录结构

```
data/
├── results/            # 实验结果(data 类章节必填)
│   ├── run_0_baseline/
│   │   └── final_info.json
│   ├── run_1_ours/
│   │   └── final_info.json
│   └── run_2_ablation/
│       └── final_info.json
├── figures/            # 图(可选):plot 脚本生成的 png,正文用文件名引用
│   ├── loss_curve.png
│   └── ablation.png
└── experiment_log.md   # 实验说明(建议):跑了什么、怎么跑的、硬件配置
```

**run 标识 = 对比表的行名**,所以取名要能自解释(`run_1_ours` 好过 `run_1`)。
两种布局都支持,框架会自动认:

| 布局 | run 标识来自 | 何时用 |
|---|---|---|
| `results/run_1_ours/final_info.json` | **目录名** | 推荐。一个 run 可以放多个文件(指标+日志+图) |
| `results/run_1_ours.json` | **文件名** | 一个 run 只有一个指标文件时 |

文件名是 `final_info` / `results` / `metrics` / `summary` 这类通用名时,取父目录名作标识;
否则取文件名。**不要**把多个 run 都命名成 `results/final_info.json` 放在同一层——
那样标识会撞在一起。

## final_info.json 格式

沿用 AI-Scientist 的约定:数字指标带 `means`(必填)和可选 `stderr` / `n`。
框架只把 `means` 写进正文,`stderr` / `n` 用来判断能不能谈显著性。

```json
{
  "run_name": "run_1_ours",
  "description": "Our method on CIFAR-100, 3 seeds",
  "hardware": "1x A100 80GB",
  "dataset": "CIFAR-100, standard split",
  "hyperparameters": "lr=0.1, batch=128, 200 epochs, SGD momentum 0.9",
  "metrics": {
    "test_accuracy":    {"means": 0.9231, "stderr": 0.0043, "n": 3},
    "train_loss":       {"means": 0.1204, "stderr": 0.0021, "n": 3},
    "wall_clock_hours": {"means": 4.2}
  }
}
```

规则:
- `means` 必须是真实跑出来的数。**缺的指标就不写,别填 0 或占位符**
- 裸数字也认(`"test_accuracy": 0.9231`),但给了 `stderr` / `n` 才能写显著性
- **文本字段(`description` / `hardware` / `dataset` / `hyperparameters`)强烈建议填**。
  它们会作为 run metadata 进上下文,防止模型臆想硬件和超参。不填的话证据挖掘会大量
  出现 "I cannot answer from the provided evidence"
- 文本字段只作**背景**,不是可引用的数字——框架会把两者分开展示

## CSV 也支持

长表(key-value)和宽表都认:

```csv
metric,value              |  model,top1,top5
test_accuracy,0.9231      |  baseline,78.3,94.1
train_loss,0.1204         |  ours,81.7,95.8
```

**多 seed / 多 run**:同一指标跑了多次,加一列 `seed`(或 `run` / `run_id` / `trial` / `fold`)。
这些身份列不会被当成指标值,而是进入结果 key(如 `exp.ours.seed=0.acc`):

```csv
method,seed,acc
ours,0,0.912
ours,1,0.918
```

身份列不必在第一列,模型/方法名等标签列可以在它后面(如 `seed,model,acc`)。身份列加标签
必须能**唯一确定每一行**:例如 `seed,lr,acc` 的超参扫描里同一 seed 出现多次,框架会报冲突
并拒绝生成 RES 目录(不猜哪一行是哪个结果)——再加一列 `run_id`(或一个文本标签列,如配置名)
区分每一行即可。

框架**不替你求均值**——论文要报均值/标准差,请自己算好放进 data/(例如另一行或 JSON 的
`means`/`stderr`)。

## 结果库有歧义时会硬停

开跑前框架生成 `data/res-ids.md`(RES 数字目录)。以下情况**拒绝生成并硬停**,报错列出
冲突的 key 和两处来源(文件:行号):

- 同一个 key 出现多个**不同**取值:多 seed 的 CSV 没有身份列、两个运行目录同名
  (如 `a/run_0/final_info.json` 与 `b/run_0/final_info.json`)、JSON 对象里重复的键;
- data/ 里有 `.json` / `.csv` 读不了(格式损坏),或某个子目录没有读权限——它的数字会悄悄缺席。

按报错加身份列 / 改名 / 修文件后重跑即可。RES 编号一经分配**永不复用**:结果从 data/
消失后(包括数值结果全部清空),其编号进 `res-ids.md` 末尾的 "Retired IDs" 列表;同一结果
重新出现时恢复原号。

## figures/

`.png` / `.pdf` / `.jpg` 会被自动扫描成可引用清单。**框架不生成图**,只写
`[FIGURE N: 标题] + Note + 数据来源 + 支撑论断`,图由你画。

## 会被忽略的文件

文件名含 `.example.` 或以 `_template` 开头的一律跳过——示例数据不会被当成真实证据。

## experiment_log.md 建议内容

- 研究问题 / 假设
- 每个 run 的意图(baseline / 消融去掉了什么 / 我们的方法)
- 数据集、评测协议、seed 数
- 已知的坑或异常

框架会把它当 Results/Discussion 的事实边界:**日志里没有的,正文不能出现。**
