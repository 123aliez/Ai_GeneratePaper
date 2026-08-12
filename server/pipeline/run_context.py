"""RunContext：一次任务执行的完整上下文（平台与引擎之间的唯一契约）。

设计要点（来自 codex 审查）：
- 现有引擎（agents/）在 import 时就冻结了 config 里的路径常量（``from config import IDEA_PATH``），
  所以多任务不能在同一个长寿命 worker 进程里靠改全局变量来切换 workspace。
- 解法是「每个任务 = 一个全新的 Python 子进程」，在 import config 之前注入
  ``PAPER_RUNTIME_ROOT`` 环境变量 + 设置 cwd。config 见到该变量就把 PROJECT_ROOT
  重定向到任务专属运行目录，所有数据路径（idea/data/workspace/references）随之级联。
- 本模块只描述「这一跑要落到哪里」，不关心引擎怎么实现——PipelineAdapter 才是执行边界。
"""
from __future__ import annotations
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class RunContext:
    """一次论文任务运行的目录契约。

    目录布局（``run_store_root`` 之下）::

        runs/{task_id}/
        ├── input/          # 不可变：来自某个 revision 的输入快照（idea/outline/data/refs）
        ├── work/           # 引擎看到的兼容视图（legacy PROJECT_ROOT）
        │   ├── idea.md
        │   ├── outline.md
        │   ├── data/
        │   ├── references/
        │   ├── workspace/  # 章节产物（final.md 等）
        │   └── skills/     # 只读符号链接 → 代码仓 skills/
        ├── output/         # 已提交的 artifact（copy 自 work 里完成校验的产物）
        ├── checkpoints/    # committed manifest（每章一份）
        └── logs/           # 原始 stdout/stderr

    平台只信任 ``output/`` 下的东西；``work/`` 是引擎的暂存区，崩溃后可重建。
    """

    user_id: str
    project_id: str
    task_id: str
    source_revision: int
    source_fingerprint: str

    run_store_root: Path    # runs/{task_id}
    input_dir: Path         # 不可变输入快照
    work_dir: Path          # 引擎兼容视图（legacy runtime root）
    output_dir: Path        # 已提交产物
    checkpoint_dir: Path    # committed manifest
    log_dir: Path           # 原始日志

    @property
    def runtime_root(self) -> Path:
        """引擎子进程的 PROJECT_ROOT。"""
        return self.work_dir

    @property
    def stdout_log(self) -> Path:
        return self.log_dir / "stdout.log"

    @property
    def stderr_log(self) -> Path:
        return self.log_dir / "stderr.log"

    def chapter_workdir(self, chapter_folder: str) -> Path:
        """某一章的工作目录（workspace/<chapter>）。"""
        return self.work_dir / "workspace" / chapter_folder

    def artifact_relpath(self, abs_path: Path) -> str:
        """把 work/ 下的绝对路径转成相对展示路径（给前端看，不暴露服务器真实路径）。"""
        try:
            return abs_path.resolve().relative_to(self.work_dir.resolve()).as_posix()
        except ValueError:
            return abs_path.name
