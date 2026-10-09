"""任务运行目录的物化（materialize）与布局。

把一个 revision 的不可变输入，铺成现有引擎认识的 legacy 目录视图，然后由
SubprocessRunPyAdapter 在该目录下起子进程跑 ``run.py``。

隔离边界（codex 审查强调）：同一台机器、同一个 Unix 用户下，仅靠 ``runs/{task_id}``
的目录命名不足以挡住 Agent 越权读写别的任务——现有 read_file/write_file 接受任意路径。
第一版的隔离靠三点叠加：
  1. 每个任务一个独立子进程，cwd 锁死在自己的 work_dir；
  2. PAPER_RUNTIME_ROOT 让 config 把所有数据路径都限定在 work_dir 内；
  3. 输入快照只读复制，引擎写产物只落在 work_dir/workspace。
更强的 OS 级隔离（mount namespace / 容器 per-task）留作后续增强，接口已预留。
"""
from __future__ import annotations
import hashlib
import os
import shutil
from pathlib import Path

from .run_context import RunContext


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def _sha256_dir(path: Path) -> str:
    """对目录算一个稳定指纹：按相对路径排序，逐文件 hash 再汇总。"""
    h = hashlib.sha256()
    for item in sorted(path.rglob("*")):
        if item.is_file():
            rel = item.relative_to(path).as_posix()
            h.update(rel.encode("utf-8"))
            h.update(b"\0")
            h.update(_sha256_file(item).encode("ascii"))
            h.update(b"\0")
    return h.hexdigest()


def compute_source_fingerprint(idea: Path, outline: Path, data: Path,
                               pipeline_version: str = "1") -> str:
    """source_fingerprint = SHA256(idea + outline + data manifest + pipeline/prompt/config 版本)。

    Resume 前必须校验它：输入变了就新建任务，而不是错误复用旧 checkpoint。
    """
    h = hashlib.sha256()
    if idea and idea.is_file():
        h.update(b"idea:")
        h.update(_sha256_file(idea).encode("ascii"))
    h.update(b"\0")
    if outline and outline.is_file():
        h.update(b"outline:")
        h.update(_sha256_file(outline).encode("ascii"))
    h.update(b"\0")
    if data and data.is_dir():
        h.update(b"data:")
        h.update(_sha256_dir(data).encode("ascii"))
    h.update(b"\0")
    h.update(b"pipeline:")
    h.update(pipeline_version.encode("utf-8"))
    return h.hexdigest()


def build_run_context(run_store_root: Path, user_id: str, project_id: str,
                      task_id: str, source_revision: int,
                      source_fingerprint: str) -> RunContext:
    """根据全局 run store 根目录 + 任务标识，组装 RunContext（只建对象，不落盘）。"""
    root = Path(run_store_root) / task_id
    return RunContext(
        user_id=user_id,
        project_id=project_id,
        task_id=task_id,
        source_revision=source_revision,
        source_fingerprint=source_fingerprint,
        run_store_root=root,
        input_dir=root / "input",
        work_dir=root / "work",
        output_dir=root / "output",
        checkpoint_dir=root / "checkpoints",
        log_dir=root / "logs",
    )


def _copy_tree(src: Path, dst: Path) -> None:
    """复制目录树，dst 已存在先清掉（输入快照要反映当前 revision）。"""
    if dst.exists():
        shutil.rmtree(dst)
    shutil.copytree(src, dst, symlinks=False, copy_function=shutil.copy2)


def materialize_input_snapshot(ctx: RunContext, inputs: dict) -> None:
    """把 revision 的输入文件物化成不可变快照（input/）。

    inputs 形如::
        {"idea": Path, "outline": Path, "outline_draft": Path|None,
         "data": Path|None, "references": Path|None}
    缺省项跳过；references 给的是目录则整目录复制（含 bibliography.md）。
    """
    ctx.input_dir.mkdir(parents=True, exist_ok=True)
    idea = inputs.get("idea")
    outline = inputs.get("outline")
    draft = inputs.get("outline_draft")
    data = inputs.get("data")
    refs = inputs.get("references")

    if idea and Path(idea).is_file():
        shutil.copy2(idea, ctx.input_dir / "idea.md")
    if outline and Path(outline).is_file():
        shutil.copy2(outline, ctx.input_dir / "outline.md")
    if draft and Path(draft).is_file():
        shutil.copy2(draft, ctx.input_dir / "outline_draft.md")
    if data and Path(data).is_dir():
        _copy_tree(Path(data), ctx.input_dir / "data")
    if refs and Path(refs).is_dir():
        _copy_tree(Path(refs), ctx.input_dir / "references")
    elif refs and Path(refs).is_file():
        # 只给了 bibliography.md 单文件：放到 references/ 下。
        (ctx.input_dir / "references").mkdir(parents=True, exist_ok=True)
        shutil.copy2(refs, ctx.input_dir / "references" / "bibliography.md")


def materialize_workdir(ctx: RunContext, skills_source: Path,
                        pre_initialized_workspace: bool = False) -> None:
    """把 input/ 快照铺成引擎认识的 work/ 视图，并补上 skills/ 符号链接。

    pre_initialized_workspace=True 时，把 input/workspace（已 --init 过的章节目录）
    一并铺进 work/workspace，让引擎直接进起草阶段而不是重新 init。
    """
    for d in (ctx.work_dir, ctx.output_dir, ctx.checkpoint_dir, ctx.log_dir):
        d.mkdir(parents=True, exist_ok=True)

    # 输入文件：从不可变快照复制到 work/（引擎的 PROJECT_ROOT）。
    for name in ("idea.md", "outline.md", "outline_draft.md"):
        src = ctx.input_dir / name
        if src.is_file():
            shutil.copy2(src, ctx.work_dir / name)
    if (ctx.input_dir / "data").is_dir():
        _copy_tree(ctx.input_dir / "data", ctx.work_dir / "data")
    if (ctx.input_dir / "references").is_dir():
        _copy_tree(ctx.input_dir / "references", ctx.work_dir / "references")
    if pre_initialized_workspace and (ctx.input_dir / "workspace").is_dir():
        _copy_tree(ctx.input_dir / "workspace", ctx.work_dir / "workspace")

    # skills/ 只读符号链接：Agent 用 read_file('skills/writing-style.md') 加载。
    # 符号链接而不是复制——skills 是代码资产，跟着仓库走，不随任务快照膨胀。
    skills_link = ctx.work_dir / "skills"
    if skills_source and Path(skills_source).is_dir():
        if skills_link.exists() or skills_link.is_symlink():
            skills_link.unlink()
        os.symlink(Path(skills_source).resolve(), skills_link, target_is_directory=True)


def cleanup_run(ctx: RunContext, keep_logs: bool = True) -> None:
    """任务结束后清理 work/（可重建的暂存区）。output/checkpoint/logs 保留。"""
    if ctx.work_dir.exists():
        shutil.rmtree(ctx.work_dir, ignore_errors=True)
    if not keep_logs and ctx.log_dir.exists():
        shutil.rmtree(ctx.log_dir, ignore_errors=True)
