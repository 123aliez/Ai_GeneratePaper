# Test: Spec Module Improves CIFAR-100 Classification

> 假数据端到端试跑用的工作区。一份 brief 覆盖整篇论文,所以在小节级声明 type,
> 框架按小节分别路由:idea 类小节读 idea.md,data 类小节读 data/。

## Type

type: method

## Central Claim
Adding a Spec module to ResNet-50 improves top-1 accuracy on CIFAR-100 by 3.4 points with negligible parameter overhead.

## Section Plan

1. **Abstract** (~150 words)
- type: abstract
- Problem: ResNet-50 on CIFAR-100. Our method: +Spec module. Headline result: 81.7% top-1 (baseline 78.3%).

2. **Introduction** (~250 words)
- type: intro
- Motivation, gap, contribution: a lightweight Spec module that boosts accuracy.

3. **Related Work** (~200 words)
- type: related
- Position against prior classification methods.

4. **Method** (~300 words)
- type: method
- Spec module design, inserted into ResNet-50. Draw the design from idea.md, not from the numbers.

5. **Results** (~300 words)
- type: results
- Report run_0_baseline vs run_1_ours vs run_2_ablation. Use the numbers from data/.

6. **Conclusion** (~150 words)
- type: conclusion
- Spec module works; ablation confirms it.
