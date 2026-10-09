"""DB session 工厂 + 建表/初始化。

支持 SQLite（离线测试/本机）和 PostgreSQL（生产），由 DATABASE_URL 决定。
SQLite 走 WAL + check_same_thread=False，让多线程 worker 与 FastAPI 共用。
"""
from __future__ import annotations
from contextlib import contextmanager
from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from server import config as platform_config
from .models import Base


_engines: dict[str, Engine] = {}
_sessionmakers: dict[str, sessionmaker] = {}


def _make_engine(url: str) -> Engine:
    if url.startswith("sqlite"):
        eng = create_engine(url, connect_args={"check_same_thread": False, "timeout": 30},
                            pool_size=20, max_overflow=10, future=True)
        # SQLite 开外键 + WAL + 长 busy_timeout（多线程 worker/heartbeat/testclient 并发写）。
        @event.listens_for(eng, "connect")
        def _set_sqlite_pragma(dbapi_conn, _):  # noqa: ANN001
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA foreign_keys=ON")
            cur.execute("PRAGMA journal_mode=WAL")
            cur.execute("PRAGMA busy_timeout=30000")  # 30s：并发写竞争时等锁而非直接报 locked
            cur.close()
        return eng
    # PostgreSQL：连接池交给默认。
    return create_engine(url, pool_pre_ping=True, future=True)


def get_engine(url: str | None = None) -> Engine:
    url = url or platform_config.DATABASE_URL
    if url not in _engines:
        _engines[url] = _make_engine(url)
    return _engines[url]


def get_sessionmaker(url: str | None = None) -> sessionmaker:
    url = url or platform_config.DATABASE_URL
    if url not in _sessionmakers:
        _sessionmakers[url] = sessionmaker(bind=get_engine(url), autoflush=False,
                                           autocommit=False, expire_on_commit=False, future=True)
    return _sessionmakers[url]


@contextmanager
def session_scope(url: str | None = None):
    """事务作用域：正常退出 commit，异常 rollback。"""
    sm = get_sessionmaker(url)
    s = sm()
    try:
        yield s
        s.commit()
    except Exception:
        s.rollback()
        raise
    finally:
        s.close()


def init_db(url: str | None = None, drop_first: bool = False) -> None:
    """创建所有表。drop_first=True 先 drop（仅离线测试用）。"""
    eng = get_engine(url)
    if drop_first:
        Base.metadata.drop_all(eng)
    Base.metadata.create_all(eng)


def reset_engines() -> None:
    """测试间切换 DATABASE_URL 时清掉缓存的 engine/sessionmaker。"""
    for eng in _engines.values():
        try:
            eng.dispose()
        except Exception:
            pass
    _engines.clear()
    _sessionmakers.clear()
