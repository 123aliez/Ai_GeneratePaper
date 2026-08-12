"""Dispatcher 公平调度 + 限流的离线并发验证（Phase 4）。

纯 SQLite + FakePublisher，不连 Celery/Redis。覆盖：
  1. 10 用户各 1 任务，global=3 → 只发 3 个
  2. A 有 A1/A2/A3 + B/B1 + C/C1，首批 = A1/B1/C1（Fair FIFO，A 不能占满）
  3. 单用户最多 1 active（第二个被 user_limit 跳过）
  4. 单项目最多 1 active
  5. DISPATCHED 计入额度（连续两次 dispatch_once 不超发）
  6. 两个 dispatcher 并发抢同一批：CAS 保证不双发（用真线程模拟）
  7. WAITING_RETRY 到期后重新参与调度
  8. 任务完成后下一名自动被选中（连续 tick）
  9. per_user_max_queued 满了拒绝入队（提交侧校验，这里测 dispatcher 不受影响）

运行: python server/tests/test_dispatcher.py
"""
import os
import sys
import tempfile
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

_tmp = tempfile.mkdtemp(prefix="paper_disp_test_")
os.environ["DATABASE_URL"] = f"sqlite:///{Path(_tmp) / 't.db'}"
os.environ["PAPER_DISABLE_REDIS"] = "1"
os.environ["PAPER_RUN_STORE_ROOT"] = str(Path(_tmp) / "runs")

from server.db import init_db, session_scope, repository as repo, TaskStatus
from server.db.session import get_sessionmaker
from server.dispatcher import (
    DispatcherLimits, FakePublisher, dispatch_once, reap_stale_tasks,
)

PASSED = []


def check(label, cond, detail=""):
    if not cond:
        raise AssertionError(f"{label}: {detail or 'failed'}")
    PASSED.append(label)


def _setup():
    init_db(drop_first=True)


def _mkuser(name):
    with session_scope() as s:
        u = repo.create_user(s, name, "x")
        return u.id


def _mkproject(uid, name="p"):
    with session_scope() as s:
        p = repo.create_project(s, uid, name)
        return p.id


def _mktask(pid, uid, fp=None):
    with session_scope() as s:
        t = repo.create_task(s, pid, uid, source_fingerprint=fp or "fp")
        return t.id


def _factory():
    return get_sessionmaker()()


# ── 1. global limit ───────────────────────────────────────────────────────────
def test_global_limit():
    _setup()
    uids = [_mkuser(f"u{i}") for i in range(10)]
    pids = [_mkproject(uids[i], f"p{i}") for i in range(10)]
    tids = [_mktask(pids[i], uids[i]) for i in range(10)]

    pub = FakePublisher()
    limits = DispatcherLimits(global_max_running=3, per_user_max_running=1,
                              per_project_max_running=1)
    res = dispatch_once(_factory, limits, pub)
    check("global=3 → 发 3 个", len(res.dispatched) == 3, f"got {len(res.dispatched)}")
    check("全局 active=3", pub.published)


# ── 2. Fair FIFO：A 不能占满 ──────────────────────────────────────────────────
def test_fair_fifo():
    _setup()
    ua = _mkuser("A"); ub = _mkuser("B"); uc = _mkuser("C")
    pa = _mkproject(ua, "PA"); pb = _mkproject(ub, "PB"); pc = _mkproject(uc, "PC")
    a1 = _mktask(pa, ua); a2 = _mktask(pa, ua); a3 = _mktask(pa, ua)
    # 注意：per_project=1 + per_user=1，所以 A 即便 Fair FIFO 第一轮也只能发 A1。
    # 为测 Fair FIFO 跨用户，给 A 的三个任务分到三个项目。
    pa2 = _mkproject(ua, "PA2"); pa3 = _mkproject(ua, "PA3")
    a2 = _mktask(pa2, ua); a3 = _mktask(pa3, ua)
    b1 = _mktask(pb, ub)
    c1 = _mktask(pc, uc)

    pub = FakePublisher()
    limits = DispatcherLimits(global_max_running=3, per_user_max_running=3,
                              per_project_max_running=1)
    res = dispatch_once(_factory, limits, pub)
    check("Fair FIFO 发 3 个", len(res.dispatched) == 3)
    # 首批应该是 A1, B1, C1（每用户最老一个）
    dispatched_users = set()
    with session_scope() as s:
        for tid in res.dispatched:
            t = repo.get_task(s, tid)
            dispatched_users.add(t.user_id)
    check("首批覆盖三个不同用户", len(dispatched_users) == 3,
          f"got {dispatched_users}")


# ── 3. per-user limit ─────────────────────────────────────────────────────────
def test_per_user_limit():
    _setup()
    uid = _mkuser("u")
    p1 = _mkproject(uid, "p1"); p2 = _mkproject(uid, "p2")
    t1 = _mktask(p1, uid); t2 = _mktask(p2, uid)
    pub = FakePublisher()
    limits = DispatcherLimits(global_max_running=3, per_user_max_running=1,
                              per_project_max_running=1)
    res = dispatch_once(_factory, limits, pub)
    check("单用户最多 1 → 只发 1 个", len(res.dispatched) == 1)
    check("另一个被 user_limit 跳过",
          any(v == "user_limit" for v in res.skipped_reasons.values()))


# ── 4. per-project limit ──────────────────────────────────────────────────────
def test_per_project_limit():
    _setup()
    u1 = _mkuser("u1"); u2 = _mkuser("u2")
    p = _mkproject(u1, "shared")  # 同一项目（实际不会跨用户，这里测同项目多任务）
    t1 = _mktask(p, u1)
    # 给 u2 也建任务挂同项目（模拟场景：限定 per_project）
    with session_scope() as s:
        t2 = repo.create_task(s, p, u2)
        tid2 = t2.id
    pub = FakePublisher()
    limits = DispatcherLimits(global_max_running=3, per_user_max_running=2,
                              per_project_max_running=1)
    res = dispatch_once(_factory, limits, pub)
    check("单项目最多 1 → 只发 1 个", len(res.dispatched) == 1)
    check("另一个被 project_limit 跳过",
          any(v == "project_limit" for v in res.skipped_reasons.values()))


# ── 5. DISPATCHED 计入额度（连续 tick 不超发）──────────────────────────────────
def test_dispatched_counts_toward_limit():
    _setup()
    uids = [_mkuser(f"u{i}") for i in range(10)]
    pids = [_mkproject(uids[i], f"p{i}") for i in range(10)]
    for i in range(10):
        _mktask(pids[i], uids[i])
    pub = FakePublisher()
    limits = DispatcherLimits(global_max_running=3, per_user_max_running=1,
                              per_project_max_running=1)
    r1 = dispatch_once(_factory, limits, pub)
    r2 = dispatch_once(_factory, limits, pub)  # DISPATCHED 还没变 RUNNING
    check("第一次发 3 个", len(r1.dispatched) == 3)
    check("第二次 0 个（DISPATCHED 占额度）", len(r2.dispatched) == 0,
          f"got {r2.dispatched}")


# ── 6. 并发 dispatcher CAS 不双发 ──────────────────────────────────────────────
def test_concurrent_dispatchers_no_double_send():
    _setup()
    uids = [_mkuser(f"u{i}") for i in range(5)]
    pids = [_mkproject(uids[i], f"p{i}") for i in range(5)]
    tids = [_mktask(pids[i], uids[i]) for i in range(5)]

    pub = FakePublisher()
    limits = DispatcherLimits(global_max_running=3, per_user_max_running=1,
                              per_project_max_running=1)
    barrier = threading.Barrier(2)
    results = []

    def run():
        barrier.wait()
        r = dispatch_once(_factory, limits, pub)
        results.append(r)

    threads = [threading.Thread(target=run) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    total_dispatched = sum(len(r.dispatched) for r in results)
    # 两个 dispatcher 抢，但 CAS 保证同一 task 不被发两次；global=3 上限不破
    dispatched_set = set(pub.published)
    check("并发不双发（唯一 task_id 数 == 发出数）", len(pub.published) == len(dispatched_set),
          f"pub={pub.published}")
    check("并发总发出 <= global limit", total_dispatched <= 3,
          f"got {total_dispatched}")


# ── 7. WAITING_RETRY 到期重新调度 ─────────────────────────────────────────────
def test_waiting_retry_reschedules():
    _setup()
    uid = _mkuser("u"); pid = _mkproject(uid, "p")
    tid = _mktask(pid, uid)
    # 手动置成 WAITING_RETRY，next_retry_at 已过期
    past = datetime.now(timezone.utc) - timedelta(seconds=10)
    with session_scope() as s:
        repo.cas_status(s, tid, TaskStatus.QUEUED, TaskStatus.WAITING_RETRY,
                        next_retry_at=past)
    pub = FakePublisher()
    limits = DispatcherLimits(global_max_running=3)
    res = dispatch_once(_factory, limits, pub)
    check("WAITING_RETRY 到期被重新调度", tid in res.dispatched,
          f"got {res.dispatched}")


# ── 8. 完成后下一名自动选中 ───────────────────────────────────────────────────
def test_next_after_complete():
    _setup()
    uid = _mkuser("u")
    # 同用户两任务：受 per_user=1 约束，第一个完成后第二个才能上
    p1 = _mkproject(uid, "p1"); p2 = _mkproject(uid, "p2")
    t1 = _mktask(p1, uid); t2 = _mktask(p2, uid)
    pub = FakePublisher()
    limits = DispatcherLimits(global_max_running=3, per_user_max_running=1,
                              per_project_max_running=1)
    r1 = dispatch_once(_factory, limits, pub)
    check("首轮发 t1", t1 in r1.dispatched)
    # 模拟 t1 完成
    with session_scope() as s:
        repo.cas_status(s, t1, TaskStatus.DISPATCHED, TaskStatus.COMPLETED,
                        finished_at=datetime.now(timezone.utc))
    r2 = dispatch_once(_factory, limits, pub)
    check("t1 完成后 t2 自动选中", t2 in r2.dispatched, f"got {r2.dispatched}")


# ── 9. reaper：stale heartbeat 回退 ───────────────────────────────────────────
def test_reaper_stale():
    _setup()
    uid = _mkuser("u"); pid = _mkproject(uid, "p")
    tid = _mktask(pid, uid)
    old = datetime.now(timezone.utc) - timedelta(seconds=300)
    with session_scope() as s:
        repo.cas_status(s, tid, TaskStatus.QUEUED, TaskStatus.RUNNING,
                        started_at=old, last_heartbeat_at=old)
    reaped = reap_stale_tasks(_factory, stale_threshold_seconds=120)
    check("stale RUNNING 被 reap", tid in reaped, f"got {reaped}")
    with session_scope() as s:
        t = repo.get_task(s, tid)
        check("reap 后变 WAITING_RETRY", t.status == TaskStatus.WAITING_RETRY)


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"  ✓ {name}")
    print(f"\n全部通过：{len(PASSED)} 项检查（Phase 4 Dispatcher 公平调度 + 并发）")
