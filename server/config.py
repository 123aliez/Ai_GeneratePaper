"""平台配置：所有 *_RUNTIME_ROOT / 并发限流 / DB URL / Session 等运行时参数。

集中一处，便于 docker-compose 用环境变量覆盖，也便于离线测试注入。
默认值是「本机开发/测试」友好的（SQLite、本地 run store），生产由 env 覆盖。
"""
from __future__ import annotations
import os
from pathlib import Path

# 仓库根（config.py 所在目录的上一层即 Ai_GeneratePaper/）。
REPO_ROOT = Path(__file__).resolve().parents[1]

# ── 存储 ────────────────────────────────────────────────────────────────────
# RUN_STORE_ROOT：所有任务的 runs/{task_id}/ 目录。
RUN_STORE_ROOT = Path(os.getenv("PAPER_RUN_STORE_ROOT") or (REPO_ROOT / "var" / "runs"))
# SKILLS_SOURCE：给任务 work/ 做只读符号链接的 skills/ 源（代码资产）。
SKILLS_SOURCE = Path(os.getenv("PAPER_SKILLS_SOURCE") or (REPO_ROOT / "skills"))

# ── 数据库 ──────────────────────────────────────────────────────────────────
# 默认 SQLite（离线测试/本机）。生产用 DATABASE_URL=postgresql+psycopg://...
DATABASE_URL = os.getenv("DATABASE_URL", f"sqlite:///{(REPO_ROOT / 'var' / 'paper.db').as_posix()}")

# ── Redis / Celery ──────────────────────────────────────────────────────────
REDIS_URL = os.getenv("REDIS_URL", "redis://127.0.0.1:6379/0")
CELERY_BROKER_URL = os.getenv("CELERY_BROKER_URL", REDIS_URL)
CELERY_RESULT_BACKEND = os.getenv("CELERY_RESULT_BACKEND", REDIS_URL)

# ── 并发限流（文档 §8/§26 默认值）──────────────────────────────────────────
GLOBAL_MAX_RUNNING_TASKS = int(os.getenv("GLOBAL_MAX_RUNNING_TASKS", "3"))
PER_USER_MAX_RUNNING_TASKS = int(os.getenv("PER_USER_MAX_RUNNING_TASKS", "1"))
PER_PROJECT_MAX_RUNNING_TASKS = int(os.getenv("PER_PROJECT_MAX_RUNNING_TASKS", "1"))
PER_USER_MAX_QUEUED_TASKS = int(os.getenv("PER_USER_MAX_QUEUED_TASKS", "3"))

# ── Dispatcher / Reaper ─────────────────────────────────────────────────────
DISPATCHER_INTERVAL_SECONDS = float(os.getenv("DISPATCHER_INTERVAL_SECONDS", "1.0"))
WORKER_HEARTBEAT_SECONDS = float(os.getenv("WORKER_HEARTBEAT_SECONDS", "30.0"))
STALE_HEARTBEAT_SECONDS = float(os.getenv("STALE_HEARTBEAT_SECONDS", "120.0"))
REAPER_INTERVAL_SECONDS = float(os.getenv("REAPER_INTERVAL_SECONDS", "15.0"))

# ── Celery Worker ───────────────────────────────────────────────────────────
CELERY_WORKER_CONCURRENCY = int(os.getenv("CELERY_WORKER_CONCURRENCY", "4"))
CELERY_PREFETCH_MULTIPLIER = int(os.getenv("CELERY_PREFETCH_MULTIPLIER", "1"))
CELERY_TASK_ACKS_LATE = os.getenv("CELERY_TASK_ACKS_LATE", "true").lower() == "true"

# ── Session ─────────────────────────────────────────────────────────────────
SESSION_TTL_SECONDS = int(os.getenv("SESSION_TTL_SECONDS", str(12 * 3600)))
SESSION_SECRET_KEY = os.getenv("SESSION_SECRET_KEY", "dev-only-change-me-in-production")

# ── 等待重试（Level 2 恢复）─────────────────────────────────────────────────
WAITING_RETRY_BASE_DELAY = float(os.getenv("WAITING_RETRY_BASE_DELAY", "30.0"))
WAITING_RETRY_MAX_DELAY = float(os.getenv("WAITING_RETRY_MAX_DELAY", "60.0"))
MAX_WAITING_RETRY_ATTEMPTS = int(os.getenv("MAX_WAITING_RETRY_ATTEMPTS", "3"))

# ── 任务执行 ─────────────────────────────────────────────────────────────────
TASK_GRACE_KILL_AFTER_SECONDS = float(os.getenv("TASK_GRACE_KILL_AFTER_SECONDS", "10.0"))


def ensure_dirs() -> None:
    """启动时创建必要的本地目录（run store / var）。"""
    RUN_STORE_ROOT.mkdir(parents=True, exist_ok=True)
    (REPO_ROOT / "var").mkdir(parents=True, exist_ok=True)
