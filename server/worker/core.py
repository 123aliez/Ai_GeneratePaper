"""Worker：Celery task + 执行核心。

设计要点（codex 审查 §2.1/§2.3）：
- Celery task 只接 ``task_id``，不传 prompt/文件/路径（broker 不扛大 payload）。
- task_id → 查 DB 确认 owner/project/status → CAS DISPATCHED→RUNNING →
  构造 RunContext + 物化 work/ → SubprocessRunPyAdapter 跑 run.py。
- 独立 heartbeat 线程：worker 进程内每 30s 更新 last_heartbeat_at（不能只靠模型输出，
  单次模型 call 可能阻塞 300/600s，否则会被 120s stale 阈值误杀）。
- cooperative cancel：cancel_check 在子进程轮询时检查 ``cancel_requested``。
- TransientModelUnavailable 的 side-channel：现有 orchestrator 把它吞成 {"error":...}，
  所以 adapter 完成后由这里看「是否所有章节都 final + 跨章交接成功」来判定 COMPLETED/FAILED；
  失败时若最后是 transient 类信号 → WAITING_RETRY，否则 FAILED。
"""
from __future__ import annotations
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from sqlalchemy import select

from server import config as platform_config
from server.db import repository as repo
from server.db.models import Task, TaskStatus
from server.db.session import session_scope
from server.pipeline import (
    build_run_context, compute_source_fingerprint,
    materialize_input_snapshot, materialize_workdir, cleanup_run,
    SubprocessRunPyAdapter, TaskManifest,
)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _load_revision_inputs(project_id: str, revision: int) -> dict:
    """从 project_files 表把某 revision 的输入文件路径聚合起来（blob 区 → dict）。"""
    with session_scope() as s:
        files = repo.list_project_files(s, project_id, revision)
    inputs: dict[str, Path | None] = {
        "idea": None, "outline": None, "outline_draft": None,
        "data": None, "references": None,
    }
    data_root = None
    refs_root = None
    for f in files:
        p = Path(f.relative_path)
        if not p.is_absolute():
            p = (Path(platform_config.REPO_ROOT) / "var" / "blobs" / p)
        if f.file_type == "idea":
            inputs["idea"] = p
        elif f.file_type == "outline":
            inputs["outline"] = p
        elif f.file_type == "outline_draft":
            inputs["outline_draft"] = p
        elif f.file_type == "data":
            data_root = p.parent if data_root is None else data_root
            inputs["data"] = p.parent
        elif f.file_type == "references":
            refs_root = p.parent if refs_root is None else refs_root
            inputs["references"] = p.parent
    return inputs


def _heartbeat_loop(task_id: str, stop: threading.Event, interval: float) -> None:
    """独立线程：定时更新 last_heartbeat_at。"""
    while not stop.wait(interval):
        try:
            with session_scope() as s:
                repo.update_heartbeat(s, task_id)
                s.commit()
        except Exception:
            # heartbeat 失败不能让 worker 挂掉。
            pass


def _is_transient_signal(outcome, ctx) -> bool:
    """判定失败是否值得 WAITING_RETRY（瞬时网关/模型问题），而非永久业务错误。

    现有 orchestrator 把异常压成 {error}，不冒泡，所以这里用「日志里出现 retry/transient
    信号 + 产物未完成」作 heuristic。后续若放开 orchestrator 异常冒泡，这里改成捕获
    TransientModelUnavailable 更精确。
    """
    try:
        text = ctx.stdout_log.read_text(encoding="utf-8", errors="replace") if ctx.stdout_log.is_file() else ""
    except Exception:
        text = ""
    transient_markers = ("retry", "TransientModelUnavailable", "APIError", "InternalServerError",
                         "stream failed", "connection reset", "503", "502", "timeout")
    low = text.lower()
    return any(m.lower() in low for m in transient_markers)


def execute_task(task_id: str,
                 adapter: SubprocessRunPyAdapter | None = None,
                 skills_source: Path | None = None) -> str:
    """执行一个 task（worker 调用入口）。返回最终状态字符串。

    流程：
      1. CAS DISPATCHED → RUNNING（拿 execution lock，重复投递直接退出）
      2. 物化 RunContext + work/
      3. 独立 heartbeat 线程 + 跑 run.py --all
      4. 判定结果 → COMPLETED / FAILED / WAITING_RETRY / CANCELLED
    """
    task_id = str(task_id)

    # 1. 取 task + CAS 进 RUNNING
    with session_scope() as s:
        t = repo.get_task(s, task_id)
        if t is None:
            return "MISSING"
        if t.status in (TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED):
            return t.status.value
        if t.status == TaskStatus.RUNNING:
            return "ALREADY_RUNNING"  # 别的 worker 在跑
        # DISPATCHED 或（罕见）QUEUED → RUNNING（CAS 防重复执行）
        now = _utcnow()
        ok = repo.cas_status_multi(s, task_id,
                                   [TaskStatus.DISPATCHED, TaskStatus.QUEUED],
                                   TaskStatus.RUNNING, started_at=now,
                                   last_heartbeat_at=now)
        if not ok:
            return "LOST_RACE"
        project_id = t.project_id
        user_id = t.user_id
        source_revision = t.source_revision
        source_fingerprint = t.source_fingerprint
        task_type = t.task_type
        repo.append_event(s, task_id, "TASK_STARTED", f"worker picked up task (type={task_type})")
        s.commit()

    # 2. 物化 RunContext
    run_root = Path(platform_config.RUN_STORE_ROOT)
    ctx = build_run_context(run_root, user_id, project_id, task_id,
                            source_revision, source_fingerprint)
    try:
        inputs = _load_revision_inputs(project_id, source_revision)
        materialize_input_snapshot(ctx, inputs)
        materialize_workdir(ctx, skills_source=skills_source or platform_config.SKILLS_SOURCE)
    except Exception as exc:
        with session_scope() as s:
            repo.cas_status(s, task_id, TaskStatus.RUNNING, TaskStatus.FAILED,
                            finished_at=_utcnow(), error_code="materialize_failed",
                            error_message=str(exc)[:500])
            repo.append_event(s, task_id, "TASK_FAILED", f"materialize failed: {exc}", level="error")
            s.commit()
        cleanup_run(ctx)
        return "FAILED"

    # 3. heartbeat 线程 + 执行
    hb_stop = threading.Event()
    hb = threading.Thread(
        target=_heartbeat_loop,
        args=(task_id, hb_stop, platform_config.WORKER_HEARTBEAT_SECONDS),
        daemon=True,
    )
    hb.start()

    def cancel_check() -> bool:
        try:
            with session_scope() as s:
                t = repo.get_task(s, task_id)
                return bool(t and t.cancel_requested)
        except Exception:
            return False

    def on_event(ev: dict) -> None:
        try:
            with session_scope() as s:
                repo.append_event(s, task_id, ev.get("event_type", "LOG"),
                                  ev.get("message", "")[:500],
                                  metadata={"stage": ev.get("stage")} if ev.get("stage") else None)
                if ev.get("stage"):
                    repo.set_current_stage(s, task_id, ev["stage"])
                s.commit()
        except Exception:
            pass

    def heartbeat_cb() -> None:
        try:
            with session_scope() as s:
                repo.update_heartbeat(s, task_id)
                s.commit()
        except Exception:
            pass

    if adapter is None:
        adapter = SubprocessRunPyAdapter(repo_root=platform_config.REPO_ROOT,
                                         heartbeat_callback=heartbeat_cb)

    def run_pipeline():
        if task_type == "run-chapter":
            # 单章：用 source_fingerprint 里约定的 chapter（简化：跑 --all 之外的章节）
            # 这里默认仍走 run-all；单章路由由 API 层在 task_type 里带 chapter。
            return adapter.run_all(ctx, event_callback=on_event, cancel_check=cancel_check)
        return adapter.run_all(ctx, event_callback=on_event, cancel_check=cancel_check)

    try:
        outcome = run_pipeline()
    except Exception as exc:
        outcome = type("R", (), {"returncode": -1, "duration_seconds": 0.0,
                                  "cancelled": False})()
        with session_scope() as s:
            repo.append_event(s, task_id, "TASK_FAILED", f"adapter crashed: {exc}", level="error")
            s.commit()
    finally:
        hb_stop.set()
        hb.join(timeout=2)

    # 4. 判定结果
    final_status = _decide_status(task_id, ctx, outcome)
    _commit_artifacts(task_id, ctx)
    return final_status


def _decide_status(task_id: str, ctx, outcome) -> str:
    """根据子进程退出码 + 产物完整性 + transient 信号，CAS 到终态。"""
    # 先看产物：work/ 下所有 final.md（每章一个）+ cross-chapter-state.md
    completed = _check_completion(ctx)
    cancelled = getattr(outcome, "cancelled", False)

    with session_scope() as s:
        t = repo.get_task(s, task_id)
        if t is None:
            return "MISSING"

        # 取消优先
        if cancelled or t.cancel_requested:
            repo.cas_status_multi(s, task_id,
                                  [TaskStatus.RUNNING, TaskStatus.DISPATCHED],
                                  TaskStatus.CANCELLED, finished_at=_utcnow())
            repo.append_event(s, task_id, "TASK_CANCELLED", "task cancelled by request")
            s.commit()
            return "CANCELLED"

        now = _utcnow()
        if completed:
            repo.cas_status_multi(s, task_id,
                                  [TaskStatus.RUNNING, TaskStatus.DISPATCHED],
                                  TaskStatus.COMPLETED, finished_at=now,
                                  progress_percent=100)
            repo.append_event(s, task_id, "TASK_COMPLETED", "all chapters finalized + cross-chapter ok")
            s.commit()
            return "COMPLETED"

        # 失败：区分瞬时 vs 永久
        attempts = (t.retry_attempt or 0) + 1
        is_transient = _is_transient_signal(outcome, ctx)
        if is_transient and attempts <= platform_config.MAX_WAITING_RETRY_ATTEMPTS:
            from datetime import timedelta
            delay = min(platform_config.WAITING_RETRY_BASE_DELAY * attempts,
                        platform_config.WAITING_RETRY_MAX_DELAY)
            next_retry = now + timedelta(seconds=delay)
            repo.cas_status_multi(s, task_id,
                                  [TaskStatus.RUNNING, TaskStatus.DISPATCHED],
                                  TaskStatus.WAITING_RETRY,
                                  retry_attempt=attempts, next_retry_at=next_retry,
                                  error_code="transient")
            repo.append_event(s, task_id, "WAITING_RETRY",
                              f"transient failure, retry #{attempts} after {delay:.0f}s",
                              level="warning")
            s.commit()
            cleanup_run(ctx, keep_logs=True)
            return "WAITING_RETRY"

        repo.cas_status_multi(s, task_id,
                              [TaskStatus.RUNNING, TaskStatus.DISPATCHED],
                              TaskStatus.FAILED, finished_at=now,
                              error_code="pipeline_failed",
                              error_message=f"returncode={getattr(outcome,'returncode',-1)}")
        repo.append_event(s, task_id, "TASK_FAILED",
                          f"pipeline failed (rc={getattr(outcome,'returncode',-1)})",
                          level="error")
        s.commit()
    cleanup_run(ctx, keep_logs=True)
    return "FAILED"


def _check_completion(ctx) -> bool:
    """work/workspace 下是否所有章节都有 final.md 且 cross-chapter-state.md 存在。

    简化判定：至少有一个 final.md，且 cross-chapter-state.md 存在。
    完整判定（每章 final + 跨章条目）由 manifest 校验，这里给一个稳健的下界。
    """
    ws = ctx.work_dir / "workspace"
    if not ws.is_dir():
        return False
    finals = list(ws.glob("*/final.md"))
    xchap = ws / "cross-chapter-state.md"
    return len(finals) > 0 and xchap.is_file()


def _commit_artifacts(task_id: str, ctx) -> None:
    """把 work/ 下完成的产物登记到 task_artifacts（sha256 + 相对路径）。"""
    import hashlib
    ws = ctx.work_dir / "workspace"
    if not ws.is_dir():
        return
    with session_scope() as s:
        for final in ws.glob("*/final.md"):
            try:
                rel = final.relative_to(ctx.work_dir).as_posix()
                sha = hashlib.sha256(final.read_bytes()).hexdigest()
                size = final.stat().st_size
                chapter = final.parent.name
                repo.add_artifact(s, task_id, "final", chapter, rel, sha, size)
            except Exception:
                pass
        # 跨章状态文件
        xchap = ws / "cross-chapter-state.md"
        if xchap.is_file():
            try:
                rel = xchap.relative_to(ctx.work_dir).as_posix()
                sha = hashlib.sha256(xchap.read_bytes()).hexdigest()
                repo.add_artifact(s, task_id, "cross_chapter_state", "__paper__", rel,
                                  sha, xchap.stat().st_size)
            except Exception:
                pass
        s.commit()


# ── Celery 接入 ──────────────────────────────────────────────────────────────
# 仅在 Redis 可连时才建 Celery app；否则 celery_app/run_task = None，调用方（CeleryPublisher）
# 自动降级为进程内直调 execute_task——这让离线测试/本机无 Redis 也能跑全链路。
def _broker_available() -> bool:
    if os.environ.get("PAPER_DISABLE_REDIS") == "1":
        return False
    try:
        import redis
        client = redis.Redis.from_url(platform_config.REDIS_URL, decode_responses=True)
        client.ping()
        client.close()
        return True
    except Exception:
        return False


celery_app = None  # type: ignore
run_task = None  # type: ignore

if _broker_available():
    try:
        from celery import Celery

        celery_app = Celery("paper", broker=platform_config.CELERY_BROKER_URL,
                            backend=platform_config.CELERY_RESULT_BACKEND)
        celery_app.conf.update(
            worker_concurrency=platform_config.CELERY_WORKER_CONCURRENCY,
            worker_prefetch_multiplier=platform_config.CELERY_PREFETCH_MULTIPLIER,
            task_acks_late=platform_config.CELERY_TASK_ACKS_LATE,
            broker_connection_retry_on_startup=True,
            task_track_started=True,
        )

        @celery_app.task(name="paper.run_task", acks_late=platform_config.CELERY_TASK_ACKS_LATE)
        def run_task(task_id: str) -> str:
            """Celery 入口：只接 task_id。"""
            return execute_task(task_id)
    except Exception:
        celery_app = None  # type: ignore
        run_task = None  # type: ignore
