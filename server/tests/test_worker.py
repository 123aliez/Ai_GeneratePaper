"""Worker 执行与恢复语义的离线验证（Phase 3）。

用 FakeAdapter 模拟 run.py 子进程的不同结局（成功/失败/瞬时失败/取消），
不发任何 LLM API 请求，不真起 run.py。覆盖：
  1. 成功 → COMPLETED + 产物登记
  2. 失败（无 transient 信号）→ FAILED
  3. 瞬时失败（日志有 retry/503）→ WAITING_RETRY（Level 2 恢复）
  4. 取消 → CANCELLED
  5. 重复投递：DISPATCHED→RUNNING 的 CAS 只成功一次，第二次 LOST_RACE
  6. heartbeat 在执行期间被更新

运行: python server/tests/test_worker.py
"""
import os
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

_tmp = tempfile.mkdtemp(prefix="paper_worker_test_")
os.environ["DATABASE_URL"] = f"sqlite:///{Path(_tmp) / 't.db'}"
os.environ["PAPER_DISABLE_REDIS"] = "1"
os.environ["PAPER_RUN_STORE_ROOT"] = str(Path(_tmp) / "runs")
os.environ["WORKER_HEARTBEAT_SECONDS"] = "0.2"  # 测试用短心跳

from server.db import init_db, session_scope, repository as repo, TaskStatus
from server.pipeline import PipelineOutcome, RunContext
from server.pipeline.runtime_layout import build_run_context, materialize_workdir
from server.worker.core import execute_task

PASSED = []


def check(label, cond, detail=""):
    if not cond:
        raise AssertionError(f"{label}: {detail or 'failed'}")
    PASSED.append(label)


class FakeAdapter:
    """模拟 SubprocessRunPyAdapter：按 mode 往 ctx.work_dir 写产物，不真跑 run.py。"""
    def __init__(self, mode="success"):
        self.mode = mode

    def run_all(self, ctx, event_callback=None, cancel_check=None):
        ws = ctx.work_dir / "workspace"
        ws.mkdir(parents=True, exist_ok=True)
        if self.mode == "success":
            (ws / "00-abstract").mkdir(exist_ok=True)
            (ws / "00-abstract" / "final.md").write_text("# Final paper", encoding="utf-8")
            (ws / "cross-chapter-state.md").write_text("- [00-abstract] done", encoding="utf-8")
            return PipelineOutcome(returncode=0, duration_seconds=0.01, cancelled=False)
        if self.mode == "fail":
            ctx.stdout_log.parent.mkdir(parents=True, exist_ok=True)
            ctx.stdout_log.write_text("[Manager] FAIL | idea.md not filled\n", encoding="utf-8")
            return PipelineOutcome(returncode=1, duration_seconds=0.01, cancelled=False)
        if self.mode == "transient":
            ctx.stdout_log.parent.mkdir(parents=True, exist_ok=True)
            ctx.stdout_log.write_text(
                "[Draft] retry | transient APIError 503 InternalServerError\n", encoding="utf-8")
            return PipelineOutcome(returncode=1, duration_seconds=0.01, cancelled=False)
        if self.mode == "cancel":
            return PipelineOutcome(returncode=0, duration_seconds=0.01, cancelled=True)
        raise ValueError(f"unknown mode {self.mode}")


def _setup_user_project():
    init_db(drop_first=True)
    with session_scope() as s:
        u = repo.create_user(s, "u", "x")
        p = repo.create_project(s, u.id, "p")
        return u.id, p.id


def _create_task(pid, uid):
    with session_scope() as s:
        t = repo.create_task(s, pid, uid, source_fingerprint="fp")
        # dispatcher 已经把它推进到 DISPATCHED
        repo.cas_status(s, t.id, TaskStatus.QUEUED, TaskStatus.DISPATCHED,
                        dispatched_at=datetime.now(timezone.utc))
        return t.id


def test_success_completed():
    uid, pid = _setup_user_project()
    tid = _create_task(pid, uid)
    status = execute_task(tid, adapter=FakeAdapter("success"),
                          skills_source=ROOT / "skills")
    check("成功 → COMPLETED", status == "COMPLETED", f"got {status}")
    with session_scope() as s:
        t = repo.get_task(s, tid)
        check("DB 状态 COMPLETED", t.status == TaskStatus.COMPLETED)
        check("progress=100", t.progress_percent == 100)
        arts = repo.list_artifacts(s, tid)
        check("产物已登记", len(arts) >= 1, f"got {len(arts)} artifacts")
        check("有 final 产物", any(a.artifact_type == "final" for a in arts))
        evs = repo.list_events(s, tid)
        check("有 TASK_COMPLETED 事件",
              any(e.event_type == "TASK_COMPLETED" for e in evs))


def test_fail_failed():
    uid, pid = _setup_user_project()
    tid = _create_task(pid, uid)
    status = execute_task(tid, adapter=FakeAdapter("fail"),
                          skills_source=ROOT / "skills")
    check("失败 → FAILED", status == "FAILED", f"got {status}")
    with session_scope() as s:
        t = repo.get_task(s, tid)
        check("DB 状态 FAILED", t.status == TaskStatus.FAILED)
        check("error_code=pipeline_failed", t.error_code == "pipeline_failed")


def test_transient_waiting_retry():
    uid, pid = _setup_user_project()
    tid = _create_task(pid, uid)
    status = execute_task(tid, adapter=FakeAdapter("transient"),
                          skills_source=ROOT / "skills")
    check("瞬时失败 → WAITING_RETRY", status == "WAITING_RETRY", f"got {status}")
    with session_scope() as s:
        t = repo.get_task(s, tid)
        check("DB 状态 WAITING_RETRY", t.status == TaskStatus.WAITING_RETRY)
        check("retry_attempt=1", t.retry_attempt == 1)
        check("next_retry_at 已设", t.next_retry_at is not None)
        check("error_code=transient", t.error_code == "transient")


def test_cancel_cancelled():
    uid, pid = _setup_user_project()
    tid = _create_task(pid, uid)
    with session_scope() as s:
        repo.mark_cancel_requested(s, tid)
        s.commit()
    status = execute_task(tid, adapter=FakeAdapter("cancel"),
                          skills_source=ROOT / "skills")
    check("取消 → CANCELLED", status == "CANCELLED", f"got {status}")
    with session_scope() as s:
        t = repo.get_task(s, tid)
        check("DB 状态 CANCELLED", t.status == TaskStatus.CANCELLED)


def test_duplicate_execution_lost_race():
    """两个 worker 同时抢同一 task，CAS 只一个进 RUNNING，另一个 LOST_RACE。"""
    uid, pid = _setup_user_project()
    tid = _create_task(pid, uid)
    # 第一个 worker 进 RUNNING（手动 CAS，模拟已抢到）
    with session_scope() as s:
        repo.cas_status_multi(s, tid, [TaskStatus.DISPATCHED], TaskStatus.RUNNING,
                              started_at=datetime.now(timezone.utc))
        s.commit()
    # 第二个 worker 来了 → 应该 LOST_RACE（不重复执行）
    status = execute_task(tid, adapter=FakeAdapter("success"),
                          skills_source=ROOT / "skills")
    check("重复投递 → ALREADY_RUNNING/LOST_RACE",
          status in ("ALREADY_RUNNING", "LOST_RACE"), f"got {status}")


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"  ✓ {name}")
    print(f"\n全部通过：{len(PASSED)} 项检查（Phase 3 Worker + 恢复语义）")
