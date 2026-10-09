"""密码 hash（bcrypt + sha256 预处理）+ Session 管理（Redis 或内存 fallback）。"""
from __future__ import annotations
import hashlib
import hmac
import os
import secrets
import time
from typing import Optional

import bcrypt as _bcrypt

from server import config as platform_config


# passlib 1.7.4 与 bcrypt>=4.1 不兼容（初始化阶段 >72 字节探针触发 ValueError），
# 因此这里直接用 bcrypt 后端 + sha256 预处理：任意长度密码 → 64 字节摘要 → bcrypt，
# 绕开 bcrypt 的 72 字节限制。hash/verify 接口语义不变。


def hash_password(plain: str) -> str:
    # sha256 预处理：任意长度密码 → 64 字节摘要 → bcrypt。salt 自带。
    digest = hashlib.sha256(plain.encode("utf-8")).digest()
    return _bcrypt.hashpw(digest, _bcrypt.gensalt()).decode("ascii")


def verify_password(plain: str, hashed: str) -> bool:
    if not hashed:
        return False
    digest = hashlib.sha256(plain.encode("utf-8")).digest()
    try:
        return hmac.compare_digest(
            _bcrypt.hashpw(digest, hashed.encode("ascii")),
            hashed.encode("ascii"),
        )
    except (ValueError, TypeError):
        return False


# ── Session 存储 ─────────────────────────────────────────────────────────────
# 生产用 Redis（持久、多 worker 共享）；离线测试/本机无 Redis 时降级到内存 dict。
class SessionStore:
    """Session 存储抽象。key=session_id, value={"user_id","username","role","created_at"}。"""

    def get(self, session_id: str) -> dict | None: ...
    def set(self, session_id: str, data: dict, ttl: int) -> None: ...
    def delete(self, session_id: str) -> None: ...
    def close(self) -> None: ...


class InMemorySessionStore(SessionStore):
    def __init__(self):
        self._d: dict[str, tuple[dict, float]] = {}

    def _gc(self):
        now = time.time()
        for k in [k for k, (_, exp) in self._d.items() if exp < now]:
            self._d.pop(k, None)

    def get(self, session_id):
        self._gc()
        rec = self._d.get(session_id)
        if not rec:
            return None
        data, exp = rec
        if exp < time.time():
            self._d.pop(session_id, None)
            return None
        return dict(data)

    def set(self, session_id, data, ttl):
        self._d[session_id] = (dict(data), time.time() + ttl)

    def delete(self, session_id):
        self._d.pop(session_id, None)

    def close(self):
        pass


class RedisSessionStore(SessionStore):
    def __init__(self, redis_client):
        self._r = redis_client
        self._prefix = "paper:sess:"

    def get(self, session_id):
        import json
        raw = self._r.get(self._prefix + session_id)
        if not raw:
            return None
        try:
            return json.loads(raw)
        except Exception:
            return None

    def set(self, session_id, data, ttl):
        import json
        self._r.setex(self._prefix + session_id, ttl, json.dumps(data, default=str))

    def delete(self, session_id):
        self._r.delete(self._prefix + session_id)

    def close(self):
        try:
            self._r.close()
        except Exception:
            pass


_store: SessionStore | None = None


def get_session_store() -> SessionStore:
    """单例 Session 存储。有 Redis 用 Redis，没有降级内存（离线测试友好）。"""
    global _store
    if _store is not None:
        return _store
    if os.getenv("PAPER_DISABLE_REDIS") == "1" or not _redis_available():
        _store = InMemorySessionStore()
        return _store
    try:
        import redis
        client = redis.Redis.from_url(platform_config.REDIS_URL, decode_responses=True)
        client.ping()
        _store = RedisSessionStore(client)
    except Exception:
        _store = InMemorySessionStore()
    return _store


def _redis_available() -> bool:
    """探测 Redis 是否真的可连。"""
    try:
        import redis
        client = redis.Redis.from_url(platform_config.REDIS_URL, decode_responses=True)
        client.ping()
        client.close()
        return True
    except Exception:
        return False


def create_session(user_id: str, username: str, role: str) -> str:
    sid = secrets.token_urlsafe(32)
    get_session_store().set(sid, {
        "user_id": str(user_id), "username": username, "role": role,
        "created_at": time.time(),
    }, platform_config.SESSION_TTL_SECONDS)
    return sid


def get_session(session_id: str) -> dict | None:
    if not session_id:
        return None
    return get_session_store().get(session_id)


def destroy_session(session_id: str) -> None:
    if session_id:
        get_session_store().delete(session_id)
