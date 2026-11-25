"""Real test backend; all mutations flow through authenticated endpoints."""
import hashlib
import hmac
import json
import secrets
import time
from pathlib import Path
from fastapi import FastAPI, Cookie, Depends, HTTPException, Request, Response
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from sqlalchemy import (Boolean, Column, Integer, MetaData, String, Table, Text,
                        create_engine, select, delete, update)
from .config import Settings
from .builds import digest


metadata = MetaData()
tasks = Table("tasks", metadata, Column("id", Integer, primary_key=True),
              Column("owner", String, nullable=False), Column("title", String, nullable=False),
              Column("completed", Boolean, nullable=False, default=False),
              Column("run_id", String, nullable=False))
sessions = Table("sessions", metadata, Column("digest", String, primary_key=True),
                 Column("identity", String, nullable=False), Column("expires", Integer, nullable=False))
logs = Table("request_logs", metadata, Column("request_id", String, primary_key=True),
             Column("method", String), Column("path", Text), Column("status", Integer), Column("timestamp", Integer))


class Login(BaseModel):
    username: str
    password: str


class NewTask(BaseModel):
    title: str = Field(min_length=1, max_length=200)


class EditTask(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=200)
    completed: bool | None = None


def create_app():
    cfg = Settings()
    source = cfg.app_source.resolve()
    manifest = json.loads((source / "manifest.json").read_text())
    if manifest["source_digest"] != digest(source):
        raise RuntimeError("Build changed after preparation")
    if (source / "backend.py").read_bytes() != Path(__file__).read_bytes():
        raise RuntimeError("Backend does not match prepared source; prepare a fresh build")
    namespace = {}
    exec(compile((source / "authorization.py").read_text(), str(source / "authorization.py"), "exec"), namespace)
    may_edit = namespace["may_edit"]
    engine = create_engine(cfg.test_database_url.get_secret_value())
    metadata.create_all(engine)
    app = FastAPI(title="QA Task Lab", version=manifest["app_version"])

    @app.middleware("http")
    async def request_log(request: Request, call_next):
        request_id = secrets.token_hex(12)
        response = await call_next(request)
        response.headers["X-Request-ID"] = request_id
        with engine.begin() as conn:
            conn.execute(logs.insert().values(request_id=request_id, method=request.method,
                path=request.url.path, status=response.status_code, timestamp=int(time.time())))
        return response

    def identity(qa_session: str | None = Cookie(default=None)):
        if not qa_session:
            raise HTTPException(401, "Authentication required")
        hashed = hashlib.sha256(qa_session.encode()).hexdigest()
        with engine.connect() as conn:
            row = conn.execute(select(sessions).where(sessions.c.digest == hashed)).mappings().first()
        if not row or row["expires"] < time.time():
            raise HTTPException(401, "Session expired or revoked")
        return row["identity"]

    def find_task(task_id):
        with engine.connect() as conn:
            row = conn.execute(select(tasks).where(tasks.c.id == task_id)).mappings().first()
        if not row:
            raise HTTPException(404, "Task not found")
        return dict(row)

    @app.get("/")
    def index():
        return FileResponse(source / "index.html")

    @app.get("/tasks.js")
    def javascript():
        return FileResponse(source / "tasks.js", media_type="text/javascript", headers={"Cache-Control": "no-store"})

    @app.get("/version")
    def version():
        return manifest

    @app.post("/api/login")
    def login(body: Login, response: Response):
        if body.username not in ("user_a", "user_b") or not hmac.compare_digest(body.password, cfg.password(body.username)):
            raise HTTPException(401, "Invalid credentials")
        token = secrets.token_urlsafe(32)
        with engine.begin() as conn:
            conn.execute(sessions.insert().values(digest=hashlib.sha256(token.encode()).hexdigest(),
                identity=body.username, expires=int(time.time()) + 3600))
        response.set_cookie("qa_session", token, httponly=True, samesite="strict")
        return {"identity": body.username}

    @app.post("/api/logout", status_code=204)
    def logout(response: Response, user: str = Depends(identity), qa_session: str = Cookie()):
        with engine.begin() as conn:
            conn.execute(delete(sessions).where(sessions.c.digest == hashlib.sha256(qa_session.encode()).hexdigest()))
        response.delete_cookie("qa_session")

    @app.get("/api/tasks")
    def list_tasks(user: str = Depends(identity)):
        with engine.connect() as conn:
            return [dict(r) for r in conn.execute(select(tasks).where(tasks.c.owner == user).order_by(tasks.c.id)).mappings()]

    @app.post("/api/tasks", status_code=201)
    def create_task(body: NewTask, request: Request, user: str = Depends(identity)):
        run_id = request.headers.get("X-QA-Run", "manual")
        with engine.begin() as conn:
            task_id = conn.execute(tasks.insert().values(owner=user, title=body.title,
                completed=False, run_id=run_id).returning(tasks.c.id)).scalar_one()
        return find_task(task_id)

    @app.get("/api/tasks/{task_id}")
    def get_task(task_id: int, user: str = Depends(identity)):
        task = find_task(task_id)
        if task["owner"] != user:
            raise HTTPException(403, "Owner required")
        return task

    @app.patch("/api/tasks/{task_id}")
    def edit_task(task_id: int, body: EditTask, user: str = Depends(identity)):
        task = find_task(task_id)
        if not may_edit(task["owner"], user):
            raise HTTPException(403, "Owner required")
        values = body.model_dump(exclude_none=True)
        if not values:
            raise HTTPException(422, "No changes supplied")
        with engine.begin() as conn:
            conn.execute(update(tasks).where(tasks.c.id == task_id).values(**values))
        return find_task(task_id)

    @app.delete("/api/tasks/{task_id}", status_code=204)
    def delete_task(task_id: int, user: str = Depends(identity)):
        task = find_task(task_id)
        if task["owner"] != user:
            raise HTTPException(403, "Owner required")
        with engine.begin() as conn:
            conn.execute(delete(tasks).where(tasks.c.id == task_id))

    return app
