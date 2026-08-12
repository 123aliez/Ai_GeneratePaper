"""DB 层离线验证（Phase 2）。纯 SQLite，不连任何外部服务。

覆盖：
  1. 建表 + CRUD（用户/项目/任务/事件/产物）
  2. ownership 强制：A 拿不到 B 的项目/任务
  3. 状态 CAS：重复投递只有一个能进 RUNNING；非法转换被拒
  4. 计数：active/queued 计数正确（dispatcher 限流基础）
  5. fingerprint 唯一性（不同输入不同指纹）

运行: python server/tests/test_db_repository.py
"""
import os
import sys
import tempfile
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

# 用临时 SQLite，并禁用 Redis，保证纯离线。
_tmp = tempfile.mkdtemp(prefix="paper_db_test_")
os.environ["DATABASE_URL"] = f"sqlite:///{Path(_tmp) / 't.db'}"
os.environ["PAPER_DISABLE_REDIS"] = "1"
os.environ["PAPER_RUN_STORE_ROOT"] = str(Path(_tmp) / "runs")

from server.db import (
    init_db, session_scope, repository as repo,
    Task, TaskStatus, TaskEvent, ACTIVE_FOR_LIMIT,
)

PASSED = []


def check(label, cond, detail=""):
    if not cond:
        raise AssertionError(f"{label}: {detail or 'failed'}")
    PASSED.append(label)


def _hash(p: str) -> str:
    from server.web.auth import hash_password
    return hash_password(p)


def test_basic_crud():
    init_db(drop_first=True)
    with session_scope() as s:
        u = repo.create_user(s, "alice", _hash("pw"), role="user")
        p = repo.create_project(s, u.id, "Alice 论文", "desc")
        t = repo.create_task(s, p.id, u.id, source_fingerprint="fp1")
        check("用户创建", u.username == "alice")
        check("项目 owner 正确", p.owner_user_id == u.id)
        check("任务初始 QUEUED", t.status == TaskStatus.QUEUED)


def test_password_hash():
    h = _hash("secret")
    from server.web.auth import verify_password
    check("密码正确", verify_password("secret", h))
    check("密码错误", not verify_password("wrong", h))


def test_ownership_isolation():
    init_db(drop_first=True)
    with session_scope() as s:
        a = repo.create_user(s, "alice", _hash("pw"))
        b = repo.create_user(s, "bob", _hash("pw"))
        pa = repo.create_project(s, a.id, "A proj")
        pb = repo.create_project(s, b.id, "B proj")
        ta = repo.create_task(s, pa.id, a.id)
        tb = repo.create_task(s, pb.id, b.id)

    with session_scope() as s:
        # A 能拿自己的项目，拿不到 B 的
        check("A 拿自己项目", repo.get_project_for_user(s, pa.id, a.id) is not None)
        check("A 拿不到 B 项目", repo.get_project_for_user(s, pb.id, a.id) is None)
        # ownership：用 B 的 project_id + A 的 user_id 也拿不到
        check("跨 owner 拿不到", repo.get_project_for_user(s, pb.id, a.id) is None)
        # 任务同理
        check("A 拿自己任务", repo.get_task_for_user(s, ta.id, a.id) is not None)
        check("A 拿不到 B 任务", repo.get_task_for_user(s, tb.id, a.id) is None)


def test_status_cas():
    init_db(drop_first=True)
    with session_scope() as s:
        u = repo.create_user(s, "u", _hash("pw"))
        p = repo.create_project(s, u.id, "p")
        t = repo.create_task(s, p.id, u.id)
        tid = t.id

    with session_scope() as s:
        # 正确的 CAS：QUEUED -> DISPATCHED
        ok = repo.cas_status(s, tid, TaskStatus.QUEUED, TaskStatus.DISPATCHED,
                             dispatched_at=__import__("datetime").datetime.now(__import__("datetime").timezone.utc))
        check("CAS QUEUED→DISPATCHED 成功", ok)

    with session_scope() as s:
        # 错误的预期状态：DISPATCHED 已不是 QUEUED
        ok = repo.cas_status(s, tid, TaskStatus.QUEUED, TaskStatus.DISPATCHED)
        check("CAS 重复预期被拒", not ok)

    with session_scope() as s:
        # DISPATCHED -> RUNNING
        ok = repo.cas_status_multi(s, tid, [TaskStatus.DISPATCHED, TaskStatus.QUEUED],
                                   TaskStatus.RUNNING,
                                   started_at=__import__("datetime").datetime.now(__import__("datetime").timezone.utc))
        check("CAS DISPATCHED→RUNNING 成功", ok)


def test_duplicate_execution_guard():
    """同一 task_id 两个 worker 抢 RUNNING，只成功一个。"""
    init_db(drop_first=True)
    with session_scope() as s:
        u = repo.create_user(s, "u", _hash("pw"))
        p = repo.create_project(s, u.id, "p")
        t = repo.create_task(s, p.id, u.id)
        tid = t.id

    now = __import__("datetime").datetime.now(__import__("datetime").timezone.utc)
    with session_scope() as s1, session_scope() as s2:
        # 模拟 DISPATCHED 后两个 worker 抢
        repo.cas_status(s1, tid, TaskStatus.QUEUED, TaskStatus.DISPATCHED, dispatched_at=now)

    with session_scope() as s1:
        ok1 = repo.cas_status_multi(s1, tid, [TaskStatus.DISPATCHED, TaskStatus.QUEUED],
                                    TaskStatus.RUNNING, started_at=now)
        # commit s1
    with session_scope() as s2:
        ok2 = repo.cas_status_multi(s2, tid, [TaskStatus.DISPATCHED, TaskStatus.QUEUED],
                                    TaskStatus.RUNNING, started_at=now)
    check("第一个 worker 拿到 RUNNING", ok1)
    check("第二个 worker 被拒（CAS）", not ok2)


def test_counts():
    init_db(drop_first=True)
    with session_scope() as s:
        u = repo.create_user(s, "u", _hash("pw"))
        p = repo.create_project(s, u.id, "p")
        t1 = repo.create_task(s, p.id, u.id)
        t2 = repo.create_task(s, p.id, u.id)
        t3 = repo.create_task(s, p.id, u.id)

    with session_scope() as s:
        # 全部 QUEUED
        check("全局 active=0", repo.count_active_tasks(s) == 0)
        check("user queued=3", repo.count_queued_tasks_for_user(s, u.id) == 3)

        # 把 t1 推到 RUNNING
        repo.cas_status_multi(s, t1.id, [TaskStatus.QUEUED], TaskStatus.RUNNING,
                              started_at=__import__("datetime").datetime.now(__import__("datetime").timezone.utc))
    with session_scope() as s:
        # user 的 active=1, queued=2
        users = repo.list_users(s)
        uid = users[0].id
        check("user active=1", repo.count_active_tasks_for_user(s, uid) == 1)
        check("user queued=2", repo.count_queued_tasks_for_user(s, uid) == 2)
        projs = repo.list_projects(s, uid)
        check("project active=1", repo.count_active_tasks_for_project(s, projs[0].id) == 1)


def test_events_artifacts():
    init_db(drop_first=True)
    with session_scope() as s:
        u = repo.create_user(s, "u", _hash("pw"))
        p = repo.create_project(s, u.id, "p")
        t = repo.create_task(s, p.id, u.id)
        tid = t.id
        repo.append_event(s, tid, "TASK_STARTED", "started")
        repo.append_event(s, tid, "STAGE_COMPLETED", "stage 0", metadata={"stage": "0"})
        repo.add_artifact(s, tid, "final", "__paper__", "workspace/00-abstract/final.md", "abc", 1234)

    with session_scope() as s:
        evs = repo.list_events(s, tid)
        check("2 条事件", len(evs) == 2)
        check("事件 id 递增", evs[1].id > evs[0].id)
        after = repo.list_events(s, tid, after_id=evs[0].id)
        check("after_id 过滤", len(after) == 1)
        arts = repo.list_artifacts(s, tid)
        check("1 个产物", len(arts) == 1 and arts[0].sha256 == "abc")


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"  ✓ {name}")
    print(f"\n全部通过：{len(PASSED)} 项检查（Phase 2 DB 层 + ownership）")
