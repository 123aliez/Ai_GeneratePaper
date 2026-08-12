"""Dispatcher：公平调度器。

职责（文档 §9）：决定「谁现在可以跑」。
- 不是串行，是公平 + 限流：从 QUEUED 任务里按 Fair FIFO 选，受 global/per-user/per-project
  并发上限约束。
- DISPATCHED 是一个独立状态：已被 dispatcher 预定 slot 但 worker 还没真正 RUNNING——
  这一步让并发额度计算精确（避免连续 tick 超发）。

实现成「纯函数 + repository + publisher」，便于离线并发测试：
  dispatch_once(now, session_factory, limits, publisher) -> 选出的 task_id 列表

publisher 是抽象：生产环境发 Celery 消息，测试用 FakePublisher 只记 task_id。
"""
from __future__ import annotations
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Iterable

from sqlalchemy import select, and_, or_
from sqlalchemy.orm import Session

from server.db import repository as repo
from server.db.models import Task, TaskStatus, ACTIVE_FOR_LIMIT


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class DispatcherLimits:
    """dispatcher 的并发上限（文档 §26 默认值，可被 platform_config 覆盖）。"""
    global_max_running: int = 3
    per_user_max_running: int = 1
    per_project_max_running: int = 1
    per_user_max_queued: int = 3


class Publisher:
    """投递抽象：把 task_id 交给执行层（Celery / 内存 worker）。"""
    def publish(self, task_id: str) -> None:  # pragma: no cover - 抽象
        raise NotImplementedError


class FakePublisher(Publisher):
    """测试用：只记下被 dispatch 的 task_id 顺序，不真的执行。"""
    def __init__(self) -> None:
        self.published: list[str] = []

    def publish(self, task_id: str) -> None:
        self.published.append(task_id)


@dataclass
class DispatchResult:
    dispatched: list[str] = field(default_factory=list)
    skipped_reasons: dict[str, str] = field(default_factory=dict)  # task_id -> 原因


def _candidate_tasks(s: Session, now: datetime) -> list[Task]:
    """选出所有可参与调度的 QUEUED 任务（WAITING_RETRY 到期的也算 QUEUED 候选）。

    WAITING_RETRY：next_retry_at <= now 的视为可重新调度——先 CAS 回 QUEUED，再参与。
    """
    # 1) WAITING_RETRY 到期 → 回 QUEUED
    due = s.scalars(select(Task).where(
        Task.status == TaskStatus.WAITING_RETRY,
        or_(Task.next_retry_at.is_(None), Task.next_retry_at <= now),
    ).order_by(Task.queue_entered_at)).all()
    for t in due:
        repo.cas_status(s, t.id, TaskStatus.WAITING_RETRY, TaskStatus.QUEUED)

    # 2) 所有 QUEUED，按入队时间排
    return list(s.scalars(select(Task).where(Task.status == TaskStatus.QUEUED)
                          .order_by(Task.queue_entered_at, Task.priority.desc())).all())


def dispatch_once(
    session_factory: Callable[[], Session],
    limits: DispatcherLimits,
    publisher: Publisher,
    now: datetime | None = None,
) -> DispatchResult:
    """单次调度：尽量填满空闲 slot，遵守 global/user/project 限额。

    Fair FIFO：先取「每个 eligible 用户最老的一个任务」，按 queue_entered_at 排序，
    让 A1/A2/A3 不会抢走 B/C 的机会。
    """
    now = now or _utcnow()
    res = DispatchResult()

    with session_factory() as s:
        candidates = _candidate_tasks(s, now)
        if not candidates:
            return res

        # 预计算「当前已 active」计数（DISPATCHED + RUNNING 都占额度）。
        # 注意：dispatch 过程中额度会被消耗，所以每轮重新计数。
        def global_active() -> int:
            return repo.count_active_tasks(s)
        def user_active(uid: str) -> int:
            return repo.count_active_tasks_for_user(s, uid)
        def project_active(pid: str) -> int:
            return repo.count_active_tasks_for_project(s, pid)

        # Fair FIFO 多轮：每轮「每个 active 未达 user/project 额度的用户」各取最老一个，
        # 按入队时间排序，依次尝试投递（投递成功即占额度）。重复直到没有候选或额度满。
        dispatched_ids: list[str] = []
        remaining = list(candidates)  # 会被逐个消费
        progressed = True
        while remaining and progressed and global_active() < limits.global_max_running:
            progressed = False
            # 本轮每用户只考虑最老的一个未处理候选。
            seen_user_round: set[str] = set()
            round_candidates: list[Task] = []
            leftover: list[Task] = []
            for t in remaining:
                if t.user_id not in seen_user_round:
                    seen_user_round.add(t.user_id)
                    round_candidates.append(t)
                else:
                    leftover.append(t)
            # 按入队时间排序本轮候选
            round_candidates.sort(key=lambda x: (x.queue_entered_at, x.priority))

            for t in round_candidates:
                if global_active() >= limits.global_max_running:
                    res.skipped_reasons[t.id] = "global_limit"
                    continue
                if user_active(t.user_id) >= limits.per_user_max_running:
                    res.skipped_reasons[t.id] = "user_limit"
                    continue
                if project_active(t.project_id) >= limits.per_project_max_running:
                    res.skipped_reasons[t.id] = "project_limit"
                    continue
                ok = repo.cas_status(s, t.id, TaskStatus.QUEUED, TaskStatus.DISPATCHED,
                                     dispatched_at=now)
                if not ok:
                    res.skipped_reasons[t.id] = "lost_race"
                    continue
                publisher.publish(t.id)
                dispatched_ids.append(t.id)
                repo.append_event(s, t.id, "DISPATCHED", "task dispatched by scheduler",
                                  metadata={"attempt": t.retry_attempt})
                progressed = True
            # 下一轮处理「每用户的第二个及以后」
            remaining = leftover
        res.dispatched = dispatched_ids
        s.commit()
    return res


# ── Reaper：清理失联（stale heartbeat）的 RUNNING 任务 ───────────────────────
def reap_stale_tasks(
    session_factory: Callable[[], Session],
    stale_threshold_seconds: float,
    now: datetime | None = None,
) -> list[str]:
    """把 heartbeat 超期的 RUNNING/DISPATCHED 任务回退到 WAITING_RETRY（释放锁）。

    返回被 reap 的 task_id 列表。worker 崩溃后由它兜底重调度。
    """
    now = now or _utcnow()
    from datetime import timedelta
    cutoff = now - timedelta(seconds=stale_threshold_seconds)
    reaped: list[str] = []
    with session_factory() as s:
        stale = list(s.scalars(select(Task).where(
            Task.status.in_([TaskStatus.RUNNING, TaskStatus.DISPATCHED]),
            Task.last_heartbeat_at.is_not(None),
            Task.last_heartbeat_at < cutoff,
        )))
        for t in stale:
            next_retry = now + timedelta(seconds=5)  # 很快重新调度（reaper 路径）
            ok = repo.cas_status_multi(
                s, t.id, [TaskStatus.RUNNING, TaskStatus.DISPATCHED],
                TaskStatus.WAITING_RETRY, next_retry_at=next_retry,
                error_code="stale_heartbeat",
            )
            if ok:
                reaped.append(t.id)
                repo.append_event(s, t.id, "STALE_REAPED",
                                  f"heartbeat stale > {stale_threshold_seconds}s, requeued",
                                  level="warning")
        s.commit()
    return reaped
