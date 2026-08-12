"""Repository 层：DB 读写 + ownership 强制 + 状态 CAS。

设计原则（codex 审查 §2.5）：
- 普通用户查询恒带 ``owner_user_id = authenticated_user``，不信任前端传来的 project_id。
- 状态转换用原子 UPDATE ... WHERE status=expected（CAS），保证重复投递/并发 dispatcher 安全。
- dispatcher 的「计数 active task」也走 SQL，避免 Python 层的 TOCTOU。
"""
from __future__ import annotations
import uuid
from datetime import datetime, timezone
from typing import Iterable

from sqlalchemy import select, update, func, and_
from sqlalchemy.orm import Session

from .models import (
    User, Project, ProjectFile, Task, TaskStatus, TaskEvent, TaskArtifact,
    TaskStageRun, LlmUsage, TERMINAL_STATUSES, ACTIVE_FOR_LIMIT, DISPATCHABLE,
)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# ── 用户 ─────────────────────────────────────────────────────────────────────
def create_user(s: Session, username: str, password_hash: str, *,
                role: str = "user", display_name: str | None = None,
                must_change_password: bool = True) -> User:
    u = User(username=username, password_hash=password_hash, role=role,
             display_name=display_name, must_change_password=must_change_password)
    s.add(u)
    s.flush()
    return u


def get_user_by_username(s: Session, username: str) -> User | None:
    return s.scalar(select(User).where(User.username == username))


def get_user(s: Session, user_id: str) -> User | None:
    return s.get(User, str(user_id))


def list_users(s: Session) -> list[User]:
    return list(s.scalars(select(User).order_by(User.created_at)))


def set_user_active(s: Session, user_id: str, is_active: bool) -> None:
    s.execute(update(User).where(User.id == str(user_id)).values(is_active=is_active))


def update_password(s: Session, user_id: str, password_hash: str,
                    *, must_change: bool = False) -> None:
    s.execute(update(User).where(User.id == str(user_id)).values(
        password_hash=password_hash, must_change_password=must_change))


def touch_login(s: Session, user_id: str) -> None:
    s.execute(update(User).where(User.id == str(user_id)).values(last_login_at=_utcnow()))


# ── 项目（ownership 强制）─────────────────────────────────────────────────────
def create_project(s: Session, owner_user_id: str, name: str,
                   description: str | None = None) -> Project:
    p = Project(owner_user_id=str(owner_user_id), name=name, description=description)
    s.add(p)
    s.flush()
    return p


def get_project_for_user(s: Session, project_id: str, user_id: str) -> Project | None:
    """ownership 强制：找不到 + 不属于该用户 都返回 None（不区分，防探测）。"""
    return s.scalar(select(Project).where(
        Project.id == str(project_id), Project.owner_user_id == str(user_id)))


def list_projects(s: Session, user_id: str) -> list[Project]:
    return list(s.scalars(select(Project)
                          .where(Project.owner_user_id == str(user_id),
                                 Project.archived_at.is_(None))
                          .order_by(Project.updated_at.desc())))


def archive_project(s: Session, project_id: str, user_id: str) -> bool:
    res = s.execute(update(Project).where(
        Project.id == str(project_id), Project.owner_user_id == str(user_id)
    ).values(archived_at=_utcnow(), status="archived"))
    return res.rowcount > 0


# ── 项目文件（revision）───────────────────────────────────────────────────────
def add_project_file(s: Session, project_id: str, revision: int, file_type: str,
                     relative_path: str, sha256: str, size_bytes: int,
                     uploaded_by: str | None = None) -> ProjectFile:
    pf = ProjectFile(project_id=str(project_id), revision=revision, file_type=file_type,
                     relative_path=relative_path, sha256=sha256, size_bytes=size_bytes,
                     uploaded_by=str(uploaded_by) if uploaded_by else None)
    s.add(pf)
    s.flush()
    return pf


def list_project_files(s: Session, project_id: str, revision: int | None = None) -> list[ProjectFile]:
    stmt = select(ProjectFile).where(ProjectFile.project_id == str(project_id))
    if revision is not None:
        stmt = stmt.where(ProjectFile.revision == revision)
    return list(s.scalars(stmt.order_by(ProjectFile.revision, ProjectFile.file_type)))


def latest_revision(s: Session, project_id: str) -> int:
    val = s.scalar(select(func.max(ProjectFile.revision))
                   .where(ProjectFile.project_id == str(project_id)))
    return int(val or 0)


def bump_revision(s: Session, project_id: str) -> int:
    return latest_revision(s, project_id) + 1


# ── 任务 ─────────────────────────────────────────────────────────────────────
def create_task(s: Session, project_id: str, user_id: str, *,
                task_type: str = "run-all", source_revision: int = 1,
                source_fingerprint: str = "", priority: int = 0,
                resume_from_task_id: str | None = None) -> Task:
    t = Task(project_id=str(project_id), user_id=str(user_id), task_type=task_type,
             status=TaskStatus.QUEUED, source_revision=source_revision,
             source_fingerprint=source_fingerprint, priority=priority,
             resume_from_task_id=resume_from_task_id)
    s.add(t)
    s.flush()
    return t


def get_task(s: Session, task_id: str) -> Task | None:
    return s.get(Task, str(task_id))


def get_task_for_user(s: Session, task_id: str, user_id: str) -> Task | None:
    """ownership：普通用户只能看自己的任务。"""
    return s.scalar(select(Task).where(Task.id == str(task_id), Task.user_id == str(user_id)))


def list_tasks_for_user(s: Session, user_id: str,
                        statuses: Iterable[TaskStatus] | None = None) -> list[Task]:
    stmt = select(Task).where(Task.user_id == str(user_id))
    if statuses:
        stmt = stmt.where(Task.status.in_(list(statuses)))
    return list(s.scalars(stmt.order_by(Task.queue_entered_at.desc())))


def list_all_tasks(s: Session, statuses: Iterable[TaskStatus] | None = None) -> list[Task]:
    stmt = select(Task)
    if statuses:
        stmt = stmt.where(Task.status.in_(list(statuses)))
    return list(s.scalars(stmt.order_by(Task.queue_entered_at.desc())))


# ── 状态 CAS：原子转换，防重复投递/并发 dispatcher ────────────────────────────
def cas_status(s: Session, task_id: str, expected: TaskStatus, new_status: TaskStatus,
               **extra) -> bool:
    """原子 ``UPDATE ... WHERE id=? AND status=expected``。返回是否真的更新了一行。"""
    values = {"status": new_status}
    values.update(extra)
    res = s.execute(update(Task).where(
        Task.id == str(task_id), Task.status == expected
    ).values(**values))
    return res.rowcount > 0


def cas_status_multi(s: Session, task_id: str,
                     expected: Iterable[TaskStatus], new_status: TaskStatus,
                     **extra) -> bool:
    """允许从多个预期状态之一转换（expected 也可直接传单个状态）。"""
    if isinstance(expected, TaskStatus):
        expected = [expected]
    values = {"status": new_status}
    values.update(extra)
    res = s.execute(update(Task).where(
        Task.id == str(task_id), Task.status.in_(list(expected))
    ).values(**values))
    return res.rowcount > 0


def mark_cancel_requested(s: Session, task_id: str) -> None:
    s.execute(update(Task).where(Task.id == str(task_id)).values(cancel_requested=True))


def update_heartbeat(s: Session, task_id: str, when: datetime | None = None) -> None:
    s.execute(update(Task).where(Task.id == str(task_id)).values(
        last_heartbeat_at=when or _utcnow()))


def set_current_stage(s: Session, task_id: str, stage: str | None,
                      progress: int | None = None) -> None:
    values: dict = {"current_stage": stage}
    if progress is not None:
        values["progress_percent"] = progress
    s.execute(update(Task).where(Task.id == str(task_id)).values(**values))


# ── 计数（dispatcher 限流用，SQL 原子）────────────────────────────────────────
def count_active_tasks(s: Session) -> int:
    """全局活跃（RUNNING + DISPATCHED）任务数。"""
    return int(s.scalar(select(func.count(Task.id)).where(Task.status.in_(list(ACTIVE_FOR_LIMIT)))) or 0)


def count_active_tasks_for_user(s: Session, user_id: str) -> int:
    return int(s.scalar(select(func.count(Task.id)).where(
        Task.user_id == str(user_id), Task.status.in_(list(ACTIVE_FOR_LIMIT)))) or 0)


def count_active_tasks_for_project(s: Session, project_id: str) -> int:
    return int(s.scalar(select(func.count(Task.id)).where(
        Task.project_id == str(project_id), Task.status.in_(list(ACTIVE_FOR_LIMIT)))) or 0)


def count_queued_tasks_for_user(s: Session, user_id: str) -> int:
    return int(s.scalar(select(func.count(Task.id)).where(
        Task.user_id == str(user_id), Task.status == TaskStatus.QUEUED)) or 0)


# ── 事件 / 产物 / stage run ───────────────────────────────────────────────────
def append_event(s: Session, task_id: str, event_type: str, message: str = "",
                 level: str = "info", metadata: dict | None = None) -> TaskEvent:
    ev = TaskEvent(task_id=str(task_id), event_type=event_type, level=level,
                   message=message, metadata_=metadata)
    s.add(ev)
    s.flush()
    return ev


def list_events(s: Session, task_id: str, after_id: int = 0) -> list[TaskEvent]:
    return list(s.scalars(select(TaskEvent).where(
        TaskEvent.task_id == str(task_id), TaskEvent.id > after_id
    ).order_by(TaskEvent.id)))


def add_artifact(s: Session, task_id: str, artifact_type: str, chapter: str,
                 relative_path: str, sha256: str, size_bytes: int) -> TaskArtifact:
    a = TaskArtifact(task_id=str(task_id), artifact_type=artifact_type, chapter=chapter,
                     relative_path=relative_path, sha256=sha256, size_bytes=size_bytes)
    s.add(a)
    s.flush()
    return a


def list_artifacts(s: Session, task_id: str) -> list[TaskArtifact]:
    return list(s.scalars(select(TaskArtifact).where(TaskArtifact.task_id == str(task_id))
                          .order_by(TaskArtifact.created_at)))


def add_stage_run(s: Session, task_id: str, stage_name: str, chapter: str,
                  status: str, **extra) -> TaskStageRun:
    sr = TaskStageRun(task_id=str(task_id), stage_name=stage_name, chapter=chapter,
                      status=status, **extra)
    s.add(sr)
    s.flush()
    return sr


def add_llm_usage(s: Session, task_id: str, **fields) -> LlmUsage:
    u = LlmUsage(task_id=str(task_id), **fields)
    s.add(u)
    s.flush()
    return u
