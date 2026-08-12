"""SQLAlchemy ORM 模型：users / projects / project_files / tasks / task_stage_runs /
task_artifacts / task_events / llm_usage。

状态机与字段语义见 multiuser_smolagents_platform_design_v1.md §3/§4/§12/§18。
ownership 校验由 repository 层强制（普通用户查询恒带 owner_user_id）。
"""
from __future__ import annotations
import enum
import uuid
from datetime import datetime, timezone

from sqlalchemy import (
    String, Text, Boolean, Integer, BigInteger, SmallInteger, Float,
    DateTime, ForeignKey, Enum, JSON, Index, text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship
from sqlalchemy.types import TypeDecorator


class _AutoIncrementBigInt(TypeDecorator):
    """跨方言的自增 BIGINT 主键：SQLite 映射成 INTEGER（自增），其它方言保持 BIGINT。

    SQLAlchemy 的 BigInteger 在 SQLite 上不自动 INTEGERPK 自增（NOT NULL 没值就插不进），
    而 PostgreSQL BIGSERIAL 由 autoincrement=True 处理。本类型让两边都对：
    SQLite emit 成 INTEGER PRIMARY KEY AUTOINCREMENT，PG/MySQL 仍是 BIGINT/BIGSERIAL。
    """
    impl = BigInteger
    cache_ok = True

    def __init__(self, **kwargs):
        kwargs.pop("autoincrement", None)
        super().__init__(**kwargs)

    def load_dialect_impl(self, dialect):
        if dialect.name == "sqlite":
            return dialect.type_descriptor(Integer())  # SQLite: INTEGER PRIMARY KEY
        return dialect.type_descriptor(BigInteger())


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _uuid() -> uuid.UUID:
    return uuid.uuid4()


class Base(DeclarativeBase):
    pass


class UserRole(str, enum.Enum):
    admin = "admin"
    user = "user"


class TaskStatus(str, enum.Enum):
    QUEUED = "QUEUED"
    DISPATCHED = "DISPATCHED"
    RUNNING = "RUNNING"
    WAITING_RETRY = "WAITING_RETRY"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


# 终态：不可再被 dispatcher 选中或 worker 改写。
TERMINAL_STATUSES = (
    TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED,
)
# 计入并发额度的活跃状态（RUNNING 真在跑 + DISPATCHED 已被 dispatcher 预定 slot）。
ACTIVE_FOR_LIMIT = (TaskStatus.RUNNING, TaskStatus.DISPATCHED)
# 可被 dispatcher 选中投递的「待跑」状态。
DISPATCHABLE = (TaskStatus.QUEUED,)


class User(Base):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(String(36), primary_key=True, default=lambda: str(_uuid()))
    username: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True)
    display_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    password_hash: Mapped[str] = mapped_column(Text, nullable=False)
    role: Mapped[UserRole] = mapped_column(Enum(UserRole), nullable=False, default=UserRole.user)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    must_change_password: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, onupdate=_utcnow)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    projects: Mapped[list["Project"]] = relationship(back_populates="owner", cascade="all, delete-orphan")


class Project(Base):
    __tablename__ = "projects"

    id: Mapped[uuid.UUID] = mapped_column(String(36), primary_key=True, default=lambda: str(_uuid()))
    owner_user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(256), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="active")
    input_revision: Mapped[int] = mapped_column(Integer, default=1)
    workspace_key: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, onupdate=_utcnow)
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    owner: Mapped["User"] = relationship(back_populates="projects")
    files: Mapped[list["ProjectFile"]] = relationship(
        back_populates="project", cascade="all, delete-orphan")
    tasks: Mapped[list["Task"]] = relationship(
        back_populates="project", cascade="all, delete-orphan")


class ProjectFile(Base):
    """某一 revision 的输入文件登记（idea/outline/data/bib）。

    真实文件存在 run store 之外的 blob 区；这里只记录元数据 + sha256，便于
    ownership 校验和 resume 时按 revision 取快照。
    """
    __tablename__ = "project_files"

    id: Mapped[uuid.UUID] = mapped_column(String(36), primary_key=True, default=lambda: str(_uuid()))
    project_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True)
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    file_type: Mapped[str] = mapped_column(String(32), nullable=False)  # idea|outline|outline_draft|data|references
    relative_path: Mapped[str] = mapped_column(Text, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, default=0)
    uploaded_by: Mapped[str | None] = mapped_column(String(36), ForeignKey("users.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    project: Mapped["Project"] = relationship(back_populates="files")


class Task(Base):
    __tablename__ = "tasks"

    id: Mapped[uuid.UUID] = mapped_column(String(36), primary_key=True, default=lambda: str(_uuid()))
    project_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True)
    user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    task_type: Mapped[str] = mapped_column(String(32), default="run-all")
    status: Mapped[TaskStatus] = mapped_column(Enum(TaskStatus), nullable=False,
                                               default=TaskStatus.QUEUED, index=True)
    source_revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    source_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    priority: Mapped[int] = mapped_column(Integer, default=0)
    current_stage: Mapped[str | None] = mapped_column(String(64), nullable=True)
    progress_percent: Mapped[int] = mapped_column(SmallInteger, default=0)
    queue_entered_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    dispatched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    cancel_requested: Mapped[bool] = mapped_column(Boolean, default=False)
    resume_from_task_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    checkpoint_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    retry_attempt: Mapped[int] = mapped_column(Integer, default=0)
    next_retry_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, onupdate=_utcnow)

    project: Mapped["Project"] = relationship(back_populates="tasks")
    events: Mapped[list["TaskEvent"]] = relationship(
        back_populates="task", cascade="all, delete-orphan", order_by="TaskEvent.id")
    artifacts: Mapped[list["TaskArtifact"]] = relationship(
        back_populates="task", cascade="all, delete-orphan")
    stage_runs: Mapped[list["TaskStageRun"]] = relationship(
        back_populates="task", cascade="all, delete-orphan")
    usage_records: Mapped[list["LlmUsage"]] = relationship(
        back_populates="task", cascade="all, delete-orphan")


class TaskStageRun(Base):
    __tablename__ = "task_stage_runs"

    id: Mapped[uuid.UUID] = mapped_column(String(36), primary_key=True, default=lambda: str(_uuid()))
    task_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False, index=True)
    stage_name: Mapped[str] = mapped_column(String(64), nullable=False)
    chapter: Mapped[str] = mapped_column(String(128), nullable=False, default="__paper__")
    status: Mapped[str] = mapped_column(String(32), default="pending")
    attempt_no: Mapped[int] = mapped_column(Integer, default=1)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    artifact_manifest: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    task: Mapped["Task"] = relationship(back_populates="stage_runs")


class TaskArtifact(Base):
    __tablename__ = "task_artifacts"

    id: Mapped[uuid.UUID] = mapped_column(String(36), primary_key=True, default=lambda: str(_uuid()))
    task_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False, index=True)
    artifact_type: Mapped[str] = mapped_column(String(64), nullable=False)
    chapter: Mapped[str] = mapped_column(String(128), nullable=False, default="__paper__")
    relative_path: Mapped[str] = mapped_column(Text, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    task: Mapped["Task"] = relationship(back_populates="artifacts")


class TaskEvent(Base):
    __tablename__ = "task_events"

    id: Mapped[int] = mapped_column(_AutoIncrementBigInt, primary_key=True, autoincrement=True)
    task_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False, index=True)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    level: Mapped[str] = mapped_column(String(16), default="info")
    message: Mapped[str] = mapped_column(Text, nullable=False, default="")
    metadata_: Mapped[dict | None] = mapped_column("metadata", JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    task: Mapped["Task"] = relationship(back_populates="events")


class LlmUsage(Base):
    __tablename__ = "llm_usage"

    id: Mapped[int] = mapped_column(_AutoIncrementBigInt, primary_key=True, autoincrement=True)
    task_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False, index=True)
    stage_name: Mapped[str | None] = mapped_column(String(64), nullable=True)
    agent_name: Mapped[str | None] = mapped_column(String(32), nullable=True)
    provider: Mapped[str | None] = mapped_column(String(32), nullable=True)
    model: Mapped[str | None] = mapped_column(String(128), nullable=True)
    logical_call_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    attempt_count: Mapped[int] = mapped_column(Integer, default=1)
    retry_count: Mapped[int] = mapped_column(Integer, default=0)
    input_tokens: Mapped[int] = mapped_column(BigInteger, default=0)
    output_tokens: Mapped[int] = mapped_column(BigInteger, default=0)
    latency_ms: Mapped[int] = mapped_column(BigInteger, default=0)
    status: Mapped[str] = mapped_column(String(32), default="ok")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    task: Mapped["Task"] = relationship(back_populates="usage_records")


# ── 索引（文档 §19）──────────────────────────────────────────────────────────
Index("ix_tasks_status_queue", Task.__table__.c.status, Task.__table__.c.queue_entered_at)
Index("ix_tasks_user_status", Task.__table__.c.user_id, Task.__table__.c.status)
Index("ix_tasks_project_status", Task.__table__.c.project_id, Task.__table__.c.status)
Index("ix_task_stage_runs_task_stage", TaskStageRun.__table__.c.task_id, TaskStageRun.__table__.c.stage_name)
Index("ix_llm_usage_task_created", LlmUsage.__table__.c.task_id, LlmUsage.__table__.c.created_at)
