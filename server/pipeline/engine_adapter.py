"""PipelineAdapter：平台与现有引擎之间的执行边界。

约束（codex 审查 §1/§2.1）：
- 「不动 agents/」。现有引擎在 import 时冻结 config 路径，所以多任务隔离靠
  「每任务一个全新 Python 子进程」：在 import config 之前注入 PAPER_RUNTIME_ROOT
  环境变量 + 设置 cwd。子进程跑的就是现有 ``run.py``（run-all / 单章）。
- 子进程的 stdout/stderr 用 OS 级 pipe 捕获（不只是 Python redirect_stdout），
  因为 ResilientModel 故意写 ``sys.__stdout__``，只有 OS pipe 能稳定接到。
- 不在子进程里 monkeypatch 全局变量，不 importlib.reload，不线程内 os.chdir。

平台只暴露两个执行入口：
  - ``run_all``    整篇模式（一个 task = 跑 run.py --all）
  - ``run_chapter``单章模式（一个 task = 跑某一章）
"""
from __future__ import annotations
import os
import re
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .run_context import RunContext


@dataclass
class PipelineOutcome:
    """引擎子进程结束后的结果摘要。"""
    returncode: int
    duration_seconds: float
    cancelled: bool = False
    last_chapter: str | None = None


class CancelledByRequest(Exception):
    """用户/平台请求取消（cooperative cancel 的边界信号）。"""


# ── stdout 归一化：把 [Manager] Stage x/y、final.md、MISSING 等关键行抽成事件 ──

_STAGE_RE = re.compile(r"\[Manager\s*\]\s*Stage\s+(\d)(?:/(\d))?", re.IGNORECASE)
_FINAL_RE = re.compile(r"(final\.md|cross-chapter-state\.md)", re.IGNORECASE)
_RETRY_RE = re.compile(r"(retry|model-retry|waiting)", re.IGNORECASE)
_COMPLETE_RE = re.compile(r"(run-all\][^\n]*complete|全部完成)", re.IGNORECASE)
_FAIL_RE = re.compile(r"(FAIL\b|未产出 final\.md|停止)", re.IGNORECASE)


def parse_log_line(line: str, last_stage: dict) -> dict | None:
    """把一行 stdout 解析成一个事件 dict（给 worker 上报 task_events）。

    返回 None 表示该行不是关键事件（普通进度/心跳）。
    last_stage 是可变 dict，跨行维持「当前 stage」状态（调用方持有）。
    """
    stripped = line.strip()
    if not stripped:
        return None
    m = _STAGE_RE.search(stripped)
    if m:
        stage_no = m.group(1)
        last_stage["stage"] = stage_no
        return {"event_type": "STAGE_LINE", "message": stripped, "stage": stage_no}
    if _RETRY_RE.search(stripped):
        return {"event_type": "MODEL_RETRY_LINE", "message": stripped}
    if _FINAL_RE.search(stripped):
        return {"event_type": "ARTIFACT_LINE", "message": stripped}
    if _COMPLETE_RE.search(stripped):
        return {"event_type": "PIPELINE_COMPLETE", "message": stripped}
    if _FAIL_RE.search(stripped):
        return {"event_type": "FAILURE_LINE", "message": stripped}
    return None


class SubprocessRunPyAdapter:
    """以独立子进程跑现有 run.py，隔离到任务的 work/ 目录。

    这是「不动 agents/」约束下最干净的隔离方式：引擎看到的 PROJECT_ROOT 被环境变量
    重定向，所有 import-time 冻结的路径常量随之落到任务目录内，互不干扰。
    """

    def __init__(self, repo_root: Path, python_executable: str | None = None,
                 heartbeat_callback: Callable[[], None] | None = None,
                 poll_interval: float = 1.0):
        self.repo_root = Path(repo_root).resolve()
        self.python = python_executable or sys.executable
        self.heartbeat_callback = heartbeat_callback
        self.poll_interval = poll_interval

    def _base_env(self, ctx: RunContext) -> dict:
        """子进程环境：注入 runtime root，让 config 重定向 PROJECT_ROOT。"""
        env = os.environ.copy()
        env["PAPER_RUNTIME_ROOT"] = str(ctx.work_dir)
        env["PAPER_MODE"] = env.get("PAPER_MODE", "experiment")
        # 关键：输入已铺进 work/，让引擎以 work 为 PROJECT_ROOT 读 idea/outline/data。
        return env

    def _stream_to_file_and_events(
        self, pipe, log_file: Path, event_callback: Callable[[dict], None] | None,
        last_stage: dict, stop_flag: threading.Event,
    ) -> None:
        """OS pipe 读取线程：按块读取、按行切分写日志 + 抽事件回调，顺带写心跳。"""
        pending = b""
        with open(log_file, "a", encoding="utf-8") as logf:
            while True:
                if stop_flag.is_set():
                    break
                chunk = pipe.read1(8192) if hasattr(pipe, "read1") else pipe.read(8192)
                if not chunk:
                    # 把残留的最后一行（无换行符）也写出去。
                    if pending:
                        self._emit_line(pending, logf, event_callback, last_stage)
                        pending = b""
                    break
                pending += chunk
                while b"\n" in pending:
                    raw, pending = pending.split(b"\n", 1)
                    self._emit_line(raw, logf, event_callback, last_stage)
                if self.heartbeat_callback:
                    self.heartbeat_callback()

    def _emit_line(self, raw: bytes, logf, event_callback, last_stage) -> None:
        try:
            line = raw.decode("utf-8", errors="replace")
        except Exception:
            line = repr(raw)
        logf.write(line + "\n")
        logf.flush()
        if event_callback:
            ev = parse_log_line(line, last_stage)
            if ev:
                event_callback(ev)

    def _run_subprocess(self, args: list[str], ctx: RunContext,
                        event_callback: Callable[[dict], None] | None,
                        cancel_check: Callable[[], bool] | None,
                        extra_env: dict | None = None) -> PipelineOutcome:
        """通用子进程执行器。args 是传给 run.py 的参数列表。"""
        cmd = [self.python, str(self.repo_root / "run.py")] + args
        env = self._base_env(ctx)
        if extra_env:
            env.update(extra_env)

        ctx.stdout_log.parent.mkdir(parents=True, exist_ok=True)
        # 记录执行命令头，便于排障（不带 key）。
        with open(ctx.stdout_log, "a", encoding="utf-8") as f:
            f.write(f"$ PAPER_RUNTIME_ROOT={ctx.work_dir} python run.py {' '.join(args)}\n")

        stop_flag = threading.Event()
        last_stage: dict = {}
        start = time.monotonic()

        proc = subprocess.Popen(
            cmd, cwd=str(ctx.work_dir), env=env,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            bufsize=-1,
        )
        cancelled = False

        def stdout_thread():
            self._stream_to_file_and_events(
                proc.stdout, ctx.stdout_log, event_callback, last_stage, stop_flag)

        def stderr_thread():
            self._stream_to_file_and_events(
                proc.stderr, ctx.stderr_log, None, last_stage, stop_flag)

        t_out = threading.Thread(target=stdout_thread, daemon=True)
        t_err = threading.Thread(target=stderr_thread, daemon=True)
        t_out.start()
        t_err.start()

        # 主循环：轮询进程 + 检查取消（cooperative cancel 的现实边界）。
        while proc.poll() is None:
            if cancel_check and cancel_check():
                cancelled = True
                stop_flag.set()
                # 先尝试 cooperative：terminate（SIGTERM），让进程自己收尾。
                proc.terminate()
                try:
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    proc.kill()
                break
            time.sleep(self.poll_interval)

        # 流收尾
        stop_flag.set()
        try:
            proc.stdout.close()
            proc.stderr.close()
        except Exception:
            pass
        t_out.join(timeout=5)
        t_err.join(timeout=5)

        duration = time.monotonic() - start
        return PipelineOutcome(
            returncode=proc.returncode if proc.returncode is not None else -1,
            duration_seconds=duration,
            cancelled=cancelled,
        )

    def run_all(self, ctx: RunContext,
                event_callback: Callable[[dict], None] | None = None,
                cancel_check: Callable[[], bool] | None = None) -> PipelineOutcome:
        """整篇模式：run.py --all --progress。"""
        return self._run_subprocess(["--all", "--progress"], ctx, event_callback, cancel_check)

    def run_chapter(self, ctx: RunContext, chapter_folder: str,
                    event_callback: Callable[[dict], None] | None = None,
                    cancel_check: Callable[[], bool] | None = None) -> PipelineOutcome:
        """单章模式：run.py <chapter> --progress。"""
        return self._run_subprocess([chapter_folder, "--progress"], ctx, event_callback, cancel_check)

    def init_workspaces(self, ctx: RunContext,
                        event_callback: Callable[[dict], None] | None = None) -> PipelineOutcome:
        """从 outline 生成章节目录（run.py --init），平台常在 run-all 前先调它。"""
        return self._run_subprocess(["--init"], ctx, event_callback, None)
