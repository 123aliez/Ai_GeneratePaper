"""FastAPI app：auth / projects / tasks / admin REST。

ownership 校验全部在路由层 + repository 双保险。Session 走 HttpOnly cookie。
不暴露任何服务器真实路径；artifact 下载经 ownership 校验后由后端读文件返回。
"""
from __future__ import annotations
import hashlib
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from fastapi import Cookie, Depends, FastAPI, HTTPException, UploadFile, File, Form, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from sqlalchemy.orm import Session

from server import config as platform_config
from server.db import repository as repo
from server.db.models import TaskStatus, UserRole
from server.db.session import session_scope, init_db
from server.pipeline.runtime_layout import compute_source_fingerprint
from server.web.auth import (
    hash_password, verify_password, create_session, get_session, destroy_session,
)
from server.dispatcher import (
    DispatcherLimits, dispatch_once, reap_stale_tasks,
)
from server.dispatcher.core import FakePublisher
from server.db.session import get_sessionmaker


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# ── Pydantic schemas ─────────────────────────────────────────────────────────
class LoginIn(BaseModel):
    username: str
    password: str


class ChangePasswordIn(BaseModel):
    old_password: str
    new_password: str


class ProjectIn(BaseModel):
    name: str
    description: Optional[str] = None


class TaskIn(BaseModel):
    task_type: str = "run-all"
    source_revision: Optional[int] = None


class UserCreateIn(BaseModel):
    username: str
    password: str
    role: str = "user"
    display_name: Optional[str] = None


# ── app 工厂 ─────────────────────────────────────────────────────────────────
def create_app(init_db_on_start: bool = True) -> FastAPI:
    app = FastAPI(title="Paper Agent Platform", version="1.0.0")
    app.add_middleware(
        CORSMiddleware, allow_origins=["*"], allow_credentials=True,
        allow_methods=["*"], allow_headers=["*"],
    )

    if init_db_on_start:
        @app.on_event("startup")
        def _startup() -> None:
            init_db()
            platform_config.ensure_dirs()

    # ── 依赖：当前用户 ───────────────────────────────────────────────────────
    def get_current_user(session_id: Optional[str] = Cookie(default=None, alias="paper_session")) -> dict:
        data = get_session(session_id) if session_id else None
        if not data:
            raise HTTPException(status_code=401, detail="not authenticated")
        with session_scope() as s:
            u = repo.get_user(s, data["user_id"])
            if not u or not u.is_active:
                raise HTTPException(status_code=401, detail="user disabled or missing")
            return {"id": u.id, "username": u.username, "role": u.role.value,
                    "must_change_password": u.must_change_password}

    def require_admin(user: dict = Depends(get_current_user)) -> dict:
        if user.get("role") != UserRole.admin.value:
            raise HTTPException(status_code=403, detail="admin required")
        return user

    def _limits() -> DispatcherLimits:
        return DispatcherLimits(
            global_max_running=platform_config.GLOBAL_MAX_RUNNING_TASKS,
            per_user_max_running=platform_config.PER_USER_MAX_RUNNING_TASKS,
            per_project_max_running=platform_config.PER_PROJECT_MAX_RUNNING_TASKS,
            per_user_max_queued=platform_config.PER_USER_MAX_QUEUED_TASKS,
        )

    def _dispatch_kick():
        """创建/取消/重排任务后触发一次调度。

        CeleryPublisher 的 publish 用 .delay()（非阻塞），所以可以在 dispatch_once
        返回后立即 publish；为避免在 dispatch 的写事务内嵌套触发执行，这里把
        publish 推迟到 dispatch_once 返回之后。
        """
        from server.dispatcher.core import DispatchResult

        class _CollectPublisher:
            """先收 task_id，dispatch_once 事务关闭后再交给真正的 publisher。"""
            def __init__(self):
                self.ids: list[str] = []
            def publish(self, task_id: str):
                self.ids.append(task_id)

        collected = _CollectPublisher()
        dispatch_once(lambda: get_sessionmaker()(), _limits(), collected)
        for tid in collected.ids:
            from server.worker.core import run_task, execute_task
            if run_task is not None:
                run_task.delay(tid)
            else:
                execute_task(tid)

    # ── Auth ─────────────────────────────────────────────────────────────────
    @app.post("/api/auth/login")
    def login(body: LoginIn):
        with session_scope() as s:
            u = repo.get_user_by_username(s, body.username)
            if not u or not u.is_active or not verify_password(body.password, u.password_hash):
                raise HTTPException(status_code=401, detail="invalid credentials")
            repo.touch_login(s, u.id)
            sid = create_session(u.id, u.username, u.role.value)
        resp = JSONResponse({"user_id": u.id, "username": u.username,
                             "role": u.role.value,
                             "must_change_password": u.must_change_password})
        resp.set_cookie("paper_session", sid,
                        httponly=True, samesite="lax",
                        secure=platform_config.SESSION_SECRET_KEY != "dev-only-change-me-in-production",
                        max_age=platform_config.SESSION_TTL_SECONDS)
        return resp

    @app.post("/api/auth/logout")
    def logout(session_id: Optional[str] = Cookie(default=None, alias="paper_session")):
        if session_id:
            destroy_session(session_id)
        resp = JSONResponse({"ok": True})
        resp.delete_cookie("paper_session")
        return resp

    @app.get("/api/auth/me")
    def me(user: dict = Depends(get_current_user)):
        return user

    @app.post("/api/auth/change-password")
    def change_password(body: ChangePasswordIn, user: dict = Depends(get_current_user)):
        with session_scope() as s:
            u = repo.get_user(s, user["id"])
            if not verify_password(body.old_password, u.password_hash):
                raise HTTPException(status_code=400, detail="old password wrong")
            repo.update_password(s, user["id"], hash_password(body.new_password),
                                 must_change=False)
        return {"ok": True}

    # ── Projects ─────────────────────────────────────────────────────────────
    @app.get("/api/projects")
    def list_projects(user: dict = Depends(get_current_user)):
        with session_scope() as s:
            ps = repo.list_projects(s, user["id"])
            return [{"id": p.id, "name": p.name, "description": p.description,
                     "status": p.status, "input_revision": p.input_revision,
                     "updated_at": p.updated_at.isoformat() if p.updated_at else None}
                    for p in ps]

    @app.post("/api/projects")
    def create_project(body: ProjectIn, user: dict = Depends(get_current_user)):
        with session_scope() as s:
            p = repo.create_project(s, user["id"], body.name, body.description)
            return {"id": p.id, "name": p.name}

    @app.get("/api/projects/{project_id}")
    def get_project(project_id: str, user: dict = Depends(get_current_user)):
        with session_scope() as s:
            p = repo.get_project_for_user(s, project_id, user["id"])
            if not p:
                raise HTTPException(status_code=404, detail="not found")
            files = repo.list_project_files(s, project_id)
            return {"id": p.id, "name": p.name, "description": p.description,
                    "status": p.status, "input_revision": p.input_revision,
                    "files": [{"file_type": f.file_type, "revision": f.revision,
                               "relative_path": f.relative_path, "size_bytes": f.size_bytes}
                              for f in files]}

    @app.post("/api/projects/{project_id}/files")
    def upload_files(project_id: str,
                     idea: UploadFile = File(None),
                     outline: UploadFile = File(None),
                     outline_draft: UploadFile = File(None),
                     data: list[UploadFile] = File(None),
                     bibliography: UploadFile = File(None),
                     user: dict = Depends(get_current_user)):
        """上传/更新输入文件 → 自动 bump 一个新 revision。"""
        with session_scope() as s:
            p = repo.get_project_for_user(s, project_id, user["id"])
            if not p:
                raise HTTPException(status_code=404, detail="not found")
            new_rev = repo.bump_revision(s, project_id)
            blob_root = Path(platform_config.REPO_ROOT) / "var" / "blobs" / project_id / f"rev_{new_rev}"
            blob_root.mkdir(parents=True, exist_ok=True)
            stored = []

            def _save(uf, ftype, rel_name):
                if uf is None:
                    return
                data_bytes = uf.file.read()
                dst = blob_root / rel_name
                dst.parent.mkdir(parents=True, exist_ok=True)
                dst.write_bytes(data_bytes)
                sha = hashlib.sha256(data_bytes).hexdigest()
                repo.add_project_file(s, project_id, new_rev, ftype,
                                      str(dst), sha, len(data_bytes), uploaded_by=user["id"])
                stored.append(ftype)

            _save(idea, "idea", "idea.md")
            _save(outline, "outline", "outline.md")
            _save(outline_draft, "outline_draft", "outline_draft.md")
            if bibliography is not None:
                _save(bibliography, "references", "references/bibliography.md")
            if data:
                for i, uf in enumerate(data):
                    data_bytes = uf.file.read()
                    dst = blob_root / "data" / (uf.filename or f"file_{i}")
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    dst.write_bytes(data_bytes)
                    sha = hashlib.sha256(data_bytes).hexdigest()
                    repo.add_project_file(s, project_id, new_rev, "data",
                                          str(dst), sha, len(data_bytes), uploaded_by=user["id"])
                    stored.append(f"data/{uf.filename}")
            p.input_revision = new_rev
            s.flush()
            return {"revision": new_rev, "stored": stored}

    # ── Tasks ────────────────────────────────────────────────────────────────
    @app.post("/api/projects/{project_id}/tasks")
    def create_task(project_id: str, body: TaskIn, user: dict = Depends(get_current_user)):
        with session_scope() as s:
            p = repo.get_project_for_user(s, project_id, user["id"])
            if not p:
                raise HTTPException(status_code=404, detail="not found")
            rev = body.source_revision or repo.latest_revision(s, project_id)
            if rev == 0:
                raise HTTPException(status_code=400, detail="no input files uploaded yet")
            # per_user_max_queued 校验
            queued = repo.count_queued_tasks_for_user(s, user["id"])
            if queued >= platform_config.PER_USER_MAX_QUEUED_TASKS:
                raise HTTPException(status_code=429, detail="per-user queued limit reached")
            # 简化：fingerprint 用 revision 的文件 sha 聚合
            files = repo.list_project_files(s, project_id, rev)
            fp_input = {"idea": None, "outline": None, "data": None}
            for f in files:
                if f.file_type == "idea":
                    fp_input["idea"] = Path(f.relative_path)
                elif f.file_type == "outline":
                    fp_input["outline"] = Path(f.relative_path)
                elif f.file_type == "data":
                    fp_input["data"] = Path(f.relative_path).parent
            fingerprint = compute_source_fingerprint(
                fp_input["idea"] or Path(""), fp_input["outline"] or Path(""),
                fp_input["data"] or Path(""))
            t = repo.create_task(s, project_id, user["id"],
                                 task_type=body.task_type, source_revision=rev,
                                 source_fingerprint=fingerprint)
            repo.append_event(s, t.id, "TASK_QUEUED", f"task created, rev={rev}")
            tid = t.id

        # 创建后立即触发一次调度（尝试填满 slot）
        _dispatch_kick()
        return {"task_id": tid, "status": "QUEUED"}

    @app.get("/api/tasks")
    def list_tasks(user: dict = Depends(get_current_user)):
        with session_scope() as s:
            ts = repo.list_tasks_for_user(s, user["id"])
            return [_task_brief(t) for t in ts]

    @app.get("/api/tasks/{task_id}")
    def get_task(task_id: str, user: dict = Depends(get_current_user)):
        with session_scope() as s:
            t = repo.get_task_for_user(s, task_id, user["id"])
            if not t:
                raise HTTPException(status_code=404, detail="not found")
            return _task_detail(t)

    @app.get("/api/tasks/{task_id}/events")
    def task_events(task_id: str, after_id: int = 0, user: dict = Depends(get_current_user)):
        with session_scope() as s:
            t = repo.get_task_for_user(s, task_id, user["id"])
            if not t:
                raise HTTPException(status_code=404, detail="not found")
            evs = repo.list_events(s, task_id, after_id=after_id)
            return [{"id": e.id, "event_type": e.event_type, "level": e.level,
                     "message": e.message, "created_at": e.created_at.isoformat() if e.created_at else None}
                    for e in evs]

    @app.get("/api/tasks/{task_id}/artifacts")
    def task_artifacts(task_id: str, user: dict = Depends(get_current_user)):
        with session_scope() as s:
            t = repo.get_task_for_user(s, task_id, user["id"])
            if not t:
                raise HTTPException(status_code=404, detail="not found")
            arts = repo.list_artifacts(s, task_id)
            return [{"id": a.id, "artifact_type": a.artifact_type, "chapter": a.chapter,
                     "relative_path": a.relative_path, "size_bytes": a.size_bytes}
                    for a in arts]

    @app.get("/api/tasks/{task_id}/artifacts/{artifact_id}/download")
    def download_artifact(task_id: str, artifact_id: str, user: dict = Depends(get_current_user)):
        """ownership 校验后返回文件内容，绝不暴露服务器真实路径。"""
        with session_scope() as s:
            t = repo.get_task_for_user(s, task_id, user["id"])
            if not t:
                raise HTTPException(status_code=404, detail="not found")
            arts = repo.list_artifacts(s, task_id)
            art = next((a for a in arts if a.id == artifact_id), None)
            if not art:
                raise HTTPException(status_code=404, detail="artifact not found")
            # artifact 的 relative_path 是相对 work/ 的；实际文件在 run store
            run_root = Path(platform_config.RUN_STORE_ROOT) / task_id
            real = run_root / "work" / art.relative_path
            if not real.is_file():
                # 也可能在 output/（committed）
                real = run_root / "output" / art.relative_path
            if not real.is_file():
                raise HTTPException(status_code=404, detail="file gone (workspace cleaned)")
            fname = Path(art.relative_path).name
            return FileResponse(str(real), filename=fname,
                                media_type="application/octet-stream")

    @app.post("/api/tasks/{task_id}/cancel")
    def cancel_task(task_id: str, user: dict = Depends(get_current_user)):
        with session_scope() as s:
            t = repo.get_task_for_user(s, task_id, user["id"])
            if not t:
                raise HTTPException(status_code=404, detail="not found")
            if t.status in (TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED):
                raise HTTPException(status_code=400, detail="task already terminal")
            repo.mark_cancel_requested(s, task_id)
            repo.append_event(s, task_id, "CANCEL_REQUESTED", "cancel requested by user")
        return {"ok": True}

    @app.post("/api/tasks/{task_id}/resume")
    def resume_task(task_id: str, user: dict = Depends(get_current_user)):
        """Resume = 新建一个任务，resume_from_task_id 指向旧的（历史可审计）。"""
        with session_scope() as s:
            old = repo.get_task_for_user(s, task_id, user["id"])
            if not old:
                raise HTTPException(status_code=404, detail="not found")
            t = repo.create_task(s, old.project_id, user["id"],
                                 task_type=old.task_type,
                                 source_revision=old.source_revision,
                                 source_fingerprint=old.source_fingerprint,
                                 resume_from_task_id=old.id)
            repo.append_event(s, t.id, "TASK_RESUMED", f"resume from {old.id}")
            new_tid = t.id
        _dispatch_kick()
        return {"task_id": new_tid, "resumed_from": task_id}

    # ── Admin ────────────────────────────────────────────────────────────────
    @app.get("/api/admin/users")
    def admin_list_users(_: dict = Depends(require_admin)):
        with session_scope() as s:
            us = repo.list_users(s)
            return [{"id": u.id, "username": u.username, "role": u.role.value,
                     "is_active": u.is_active, "display_name": u.display_name}
                    for u in us]

    @app.post("/api/admin/users")
    def admin_create_user(body: UserCreateIn, _: dict = Depends(require_admin)):
        with session_scope() as s:
            if repo.get_user_by_username(s, body.username):
                raise HTTPException(status_code=409, detail="username exists")
            u = repo.create_user(s, body.username, hash_password(body.password),
                                 role=body.role, display_name=body.display_name,
                                 must_change_password=True)
            return {"id": u.id, "username": u.username}

    @app.patch("/api/admin/users/{user_id}")
    def admin_update_user(user_id: str, is_active: bool = None,
                          _: dict = Depends(require_admin)):
        with session_scope() as s:
            if is_active is not None:
                repo.set_user_active(s, user_id, is_active)
        return {"ok": True}

    @app.get("/api/admin/tasks")
    def admin_list_tasks(_: dict = Depends(require_admin)):
        with session_scope() as s:
            ts = repo.list_all_tasks(s)
            return [_task_brief(t) for t in ts]

    @app.post("/api/admin/tasks/{task_id}/requeue")
    def admin_requeue(task_id: str, _: dict = Depends(require_admin)):
        with session_scope() as s:
            ok = repo.cas_status(s, task_id, TaskStatus.FAILED, TaskStatus.QUEUED)
            if not ok:
                raise HTTPException(status_code=400, detail="task not FAILED")
            repo.append_event(s, task_id, "ADMIN_REQUEUE", "requeued by admin")
        _dispatch_kick()
        return {"ok": True}

    @app.get("/api/admin/system")
    def admin_system(_: dict = Depends(require_admin)):
        with session_scope() as s:
            return {
                "global_running": repo.count_active_tasks(s),
                "limits": {
                    "global_max_running": platform_config.GLOBAL_MAX_RUNNING_TASKS,
                    "per_user_max_running": platform_config.PER_USER_MAX_RUNNING_TASKS,
                    "per_project_max_running": platform_config.PER_PROJECT_MAX_RUNNING_TASKS,
                },
            }

    @app.post("/api/admin/reap")
    def admin_reap(_: dict = Depends(require_admin)):
        reaped = reap_stale_tasks(lambda: get_sessionmaker()(),
                                  platform_config.STALE_HEARTBEAT_SECONDS)
        return {"reaped": reaped}

    @app.get("/api/health")
    def health():
        return {"ok": True}

    # 静态前端（纯原生 HTML/JS，无构建步骤；生产可换 React+Vite）
    static_dir = Path(__file__).parent / "static"
    if static_dir.is_dir():
        app.mount("/", StaticFiles(directory=str(static_dir), html=True), name="static")

    return app


def _task_brief(t) -> dict:
    return {"id": t.id, "project_id": t.project_id, "status": t.status.value,
            "task_type": t.task_type, "current_stage": t.current_stage,
            "progress_percent": t.progress_percent,
            "created_at": t.created_at.isoformat() if t.created_at else None,
            "queue_entered_at": t.queue_entered_at.isoformat() if t.queue_entered_at else None,
            "started_at": t.started_at.isoformat() if t.started_at else None,
            "finished_at": t.finished_at.isoformat() if t.finished_at else None}


def _task_detail(t) -> dict:
    base = _task_brief(t)
    base.update({
        "retry_attempt": t.retry_attempt,
        "error_code": t.error_code,
        "error_message": t.error_message,
        "cancel_requested": t.cancel_requested,
    })
    return base


# 模块级 app（uvicorn 直接用）
app = create_app(init_db_on_start=True)
