"""Loopback-only control plane for the existing QA runners and evidence store."""
import asyncio
from contextlib import asynccontextmanager
import ctypes
from ctypes import wintypes
import json
import os
from pathlib import Path
import re
import secrets
import subprocess
import sys
import threading
import time
from uuid import uuid4

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator
from sqlalchemy import create_engine, text

from .builds import digest
from .config import Settings

ASSETS = Path(__file__).with_name("dashboard_assets")
SECRETS = ("test_database_url", "verifier_database_url", "agent_state_database_url",
           "test_user_a_password", "test_user_b_password", "llm_api_key", "api_auth_value")
ROLES = ("ui_explorer", "api_explorer", "auth_db_explorer", "diagnosis")


def protect(data: bytes, decrypt=False) -> bytes:
    """Windows DPAPI: encrypted configuration is bound to the current OS user."""
    if os.name != "nt":
        raise ValueError("Secure saved credentials require Windows DPAPI on this installation")

    class Blob(ctypes.Structure):
        _fields_ = [("size", wintypes.DWORD), ("data", ctypes.POINTER(ctypes.c_ubyte))]

    buffer = ctypes.create_string_buffer(data)
    source = Blob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
    result = Blob()
    crypt = ctypes.windll.crypt32
    operation = crypt.CryptUnprotectData if decrypt else crypt.CryptProtectData
    if not operation(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(result)):
        raise ValueError("Unable to unlock project credentials for this Windows account")
    try:
        return ctypes.string_at(result.data, result.size)
    finally:
        ctypes.windll.kernel32.LocalFree(result.data)


class Project(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    name: str = Field(default="Task application", min_length=1, max_length=100)
    app_url: str = "http://127.0.0.1:8000"
    app_source: str = ".local/builds/clean"
    repository_url: str = ""
    source_version: str = ""
    api_url: str = "http://127.0.0.1:8000"
    api_spec_url: str = "http://127.0.0.1:8000/openapi.json"
    api_auth_header: str = "Authorization"
    owner_username: str = "user_a"
    owner_role: str = "Task owner"
    other_username: str = "user_b"
    other_role: str = "Other authenticated user"
    requirements: str = Field(default="Saved tasks persist after reload. Only the owner may view, edit, or delete a task. Logout revokes the session.", max_length=12000)
    mode: str = "llm"
    workflow: str = "task_lifecycle"
    max_actions: int = Field(default=60, ge=1, le=500)
    max_seconds: int = Field(default=600, ge=10, le=7200)
    max_model_tokens: int = Field(default=60000, ge=1000, le=1000000)
    browser_headless: bool = True
    llm_provider: str = "groq"
    llm_model: str = "qwen/qwen3.8-27b"
    llm_base_url: str = ""
    llm_reasoning_effort: str = "none"
    secret_updates: dict[str, str] = Field(default_factory=dict, exclude=True)

    @field_validator("app_url", "api_url", "api_spec_url", "llm_base_url", "repository_url")
    @classmethod
    def valid_url(cls, value):
        if not value:
            return value
        try:
            url = httpx.URL(value)
            if url.scheme not in ("http", "https") or not url.host or url.username or url.password:
                raise ValueError()
        except Exception:
            raise ValueError("Use an HTTP(S) URL without embedded credentials") from None
        return value.rstrip("/")


def read_json(path, fallback=None):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return fallback


class Dashboard:
    def __init__(self, cfg, root=None):
        self.cfg = cfg
        self.root = Path(root or ".local/dashboard").resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.lock = threading.Lock()
        self.process = None
        self.current_id = None
        self.session = secrets.token_urlsafe(32)
        self.projects_file = self.root / "projects.bin"
        self.projects = {}
        if self.projects_file.exists():
            self.projects = json.loads(protect(self.projects_file.read_bytes(), decrypt=True))
        for path in sorted(self.root.glob("run_*/worker.json"), key=lambda p: p.stat().st_mtime, reverse=True):
            worker = read_json(path, {})
            if worker.get("status") == "RUNNING" and time.time() - worker.get("heartbeat", 0) < 10:
                self.current_id = path.parent.name
                break

    def defaults(self):
        cfg = self.cfg
        return Project(app_url=cfg.app_url, app_source=str(cfg.app_source),
                       api_url=cfg.api_url or cfg.app_url,
                       api_spec_url=cfg.api_spec_url or (cfg.api_url or cfg.app_url) + "/openapi.json",
                       llm_provider=cfg.llm_provider, llm_model=cfg.llm_model,
                       llm_base_url=cfg.llm_base_url, llm_reasoning_effort=cfg.llm_reasoning_effort,
                       max_actions=cfg.max_actions, max_seconds=cfg.max_seconds,
                       max_model_tokens=cfg.max_model_tokens, workflow=cfg.workflow).model_dump()

    def public_project(self, project_id):
        stored = self.projects[project_id]
        result = dict(stored["config"])
        result["id"] = project_id
        result["secrets_configured"] = {key: bool(stored["secrets"].get(key) or
            getattr(self.cfg, key).get_secret_value()) for key in SECRETS}
        return result

    def save(self, project, project_id=None):
        project_id = project_id or "project_" + uuid4().hex[:12]
        previous = dict(self.projects.get(project_id, {}).get("secrets", {}))
        if set(project.secret_updates) - set(SECRETS):
            raise ValueError("Unknown credential field")
        for key, value in project.secret_updates.items():
            if value:
                previous[key] = value
        updated = {**self.projects, project_id: {"config": project.model_dump(), "secrets": previous}}
        temp = self.root / "projects.tmp"
        temp.write_bytes(protect(json.dumps(updated).encode()))
        temp.replace(self.projects_file)
        self.projects = updated
        return self.public_project(project_id)

    def settings_for(self, project_id):
        stored = self.projects[project_id]
        project = Project(**stored["config"])
        if project.workflow not in ("task_lifecycle", "authentication") or project.mode not in ("llm", "baseline", "random"):
            raise ValueError("Choose a supported workflow and execution mode")
        if project.workflow == "authentication" and project.mode == "random":
            raise ValueError("Authentication supports autonomous agents or the fixed verification suite")
        if (project.owner_username, project.other_username) != ("user_a", "user_b"):
            raise ValueError("Task lifecycle requires the user_a and user_b test identities")
        for value in (project.app_url, project.api_url, project.api_spec_url):
            url = httpx.URL(value)
            if url.scheme not in ("http", "https") or not url.host or url.username or url.password:
                raise ValueError("Enter HTTP(S) URLs without embedded credentials")
        if not re.fullmatch(r"[A-Za-z0-9-]+", project.api_auth_header) or project.api_auth_header.lower() in ("host", "cookie", "content-length", "x-qa-run"):
            raise ValueError("Choose an API authentication header such as Authorization or X-API-Key")
        fields = {key: value for key, value in project.model_dump().items() if key in Settings.model_fields}
        fields.update(stored["secrets"])
        fields["requirements"] += f"\nAccount roles: user_a = {project.owner_role}; user_b = {project.other_role}."
        values = {key: (value.get_secret_value() if hasattr(value, "get_secret_value") else value)
                  for key, value in self.cfg.__dict__.items()}
        return project, Settings(**{**values, **fields})

    def preflight(self, project_id):
        project, cfg = self.settings_for(project_id)
        source = cfg.app_source.resolve()
        manifest = read_json(source / "manifest.json")
        if not manifest or digest(source) != manifest.get("source_digest"):
            raise ValueError("Select an intact prepared source folder with manifest.json")
        if project.source_version and project.source_version not in (manifest["source_commit"], manifest["app_version"], manifest["source_digest"]):
            raise ValueError("Expected version does not match the selected source folder")
        if project.mode == "llm" and not cfg.llm_api_key.get_secret_value():
            raise ValueError("Add a model API key in Credentials before starting agents")
        headers = {cfg.api_auth_header: cfg.api_auth_value.get_secret_value()} if cfg.api_auth_value.get_secret_value() else {}
        with httpx.Client(timeout=5, headers=headers) as client:
            response = client.get(cfg.app_url + "/version")
            response.raise_for_status()
            if response.json() != manifest:
                raise ValueError("Website deployment and source version do not match")
            response = client.get(cfg.api_spec_url)
            response.raise_for_status()
            paths = response.json().get("paths", {})
            if not {"/api/login", "/api/tasks", "/api/tasks/{task_id}"}.issubset(paths):
                raise ValueError("API specification does not support the selected task lifecycle workflow")
            response = client.get(cfg.api_url + "/version")
            response.raise_for_status()
            if response.json() != manifest:
                raise ValueError("API deployment and source version do not match")
        for field in ("test_database_url", "agent_state_database_url", "verifier_database_url"):
            engine = create_engine(getattr(cfg, field).get_secret_value(), connect_args={"connect_timeout": 5})
            try:
                with engine.connect() as conn:
                    conn.execute(text("SELECT 1"))
                    if field == "verifier_database_url":
                        if conn.execute(text("SHOW default_transaction_read_only")).scalar_one() != "on":
                            raise ValueError("Verifier connection must use a read-only role")
                        conn.execute(text("SELECT id FROM tasks LIMIT 1"))
            finally:
                engine.dispose()
        return cfg

    def running(self):
        if self.process is not None:
            return self.process.poll() is None
        if self.current_id:
            worker = read_json(self.root / self.current_id / "worker.json", {})
            return worker.get("status") == "RUNNING" and time.time() - worker.get("heartbeat", 0) < 10
        return False

    def start(self, project_id):
        with self.lock:
            if self.running():
                raise ValueError("A run is already active. Stop it before starting another")
            cfg = self.preflight(project_id)
            run_id = "run_" + uuid4().hex[:12]
            control = self.root / run_id
            control.mkdir()
            payload = {key: (value.get_secret_value() if hasattr(value, "get_secret_value") else str(value) if isinstance(value, Path) else value)
                       for key, value in cfg.__dict__.items()}
            payload.update(dashboard_run_id=run_id, stop_file=str(control / "stop"), live_stream_dir=str(control))
            metadata = {"project_id": project_id, "project_name": self.projects[project_id]["config"]["name"],
                        "started_at": time.time(), "run_id": run_id, "app_url": cfg.app_url,
                        "workflow": cfg.workflow,
                        "limits": {"actions": cfg.max_actions, "seconds": cfg.max_seconds, "model_tokens": cfg.max_model_tokens}}
            (control / "control.json").write_text(json.dumps(metadata), encoding="utf-8")
            # Worker output is not served: only redacted structured evidence is exposed.
            self.process = subprocess.Popen([sys.executable, "-m", "qa_poc.dashboard_worker"], stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, text=True,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
            self.process.stdin.write(json.dumps({"settings": payload, "mode": self.projects[project_id]["config"]["mode"]}) + "\n")
            self.process.stdin.close()
            self.current_id = run_id
            return {"run_id": run_id}

    def stop(self):
        with self.lock:
            if self.running():
                (self.root / self.current_id / "stop").touch()
            return {"status": "STOPPING" if self.running() else "IDLE"}

    def folder(self, run_id):
        if not re.fullmatch(r"run_[a-f0-9]{12}", run_id):
            raise HTTPException(404, "Run not found")
        return self.cfg.runs_dir.resolve() / run_id

    def snapshot(self, run_id):
        folder = self.folder(run_id)
        control = self.root / run_id
        metadata = read_json(control / "control.json", {})
        board = read_json(folder / "shared_state.json", {})
        report = read_json(folder / "report.json")
        active = run_id == self.current_id and self.running()
        status = ("STOPPING" if (control / "stop").exists() else "RUNNING") if active else ("COMPLETED" if report else "INTERRUPTED")
        if active and not board:
            status = "STARTING" if not (control / "stop").exists() else "STOPPING"
        events = []
        for name in ("tool_events.jsonl", "provider_events.jsonl", "browser_events.jsonl", "errors.jsonl"):
            try:
                for line in (folder / name).read_text(encoding="utf-8").splitlines()[-100:]:
                    try:
                        entry = json.loads(line)
                        entry["kind"] = name.removesuffix(".jsonl")
                        events.append(entry)
                    except ValueError:
                        pass
            except OSError:
                pass
        events.sort(key=lambda item: item.get("timestamp", ""))
        assertions = report["assertions"] if report else list(board.get("assertions", {}).values())
        return {"run_id": run_id, "status": status, "metadata": metadata,
                "mode": report.get("execution_mode") if report else board.get("mode"),
                "started_at": board.get("started_at"), "agent_status": board.get("agent_status", {}),
                "assertions": assertions, "budgets": board.get("budgets", {}), "events": events[-100:],
                "stop_reason": report.get("stop_reason", "") if report else board.get("stop_reason", ""),
                "report_available": report is not None, "frame": read_json(control / "frame.json")}


def create_app(cfg=None, root=None):
    state = Dashboard(cfg or Settings(), root)

    @asynccontextmanager
    async def lifespan(app):
        yield
        state.stop()
        if state.running() and state.process is not None:
            # A bounded model request may still be returning; allow cleanup to finish.
            await asyncio.to_thread(state.process.wait, timeout=90)

    app = FastAPI(title="QA control", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.dashboard = state

    @app.middleware("http")
    async def guard(request, call_next):
        host = request.url.hostname
        if host not in ("127.0.0.1", "localhost", "testserver"):
            return JSONResponse({"detail": "Dashboard is available on localhost only"}, status_code=403)
        origin = request.headers.get("origin")
        if origin and origin != str(request.base_url).rstrip("/"):
            return JSONResponse({"detail": "Cross-origin requests are disabled"}, status_code=403)
        if request.url.path.startswith("/api/"):
            if not secrets.compare_digest(request.cookies.get("qa_dashboard", ""), state.session):
                return JSONResponse({"detail": "Open the dashboard to start a local session"}, status_code=401)
            if request.method in ("POST", "PUT", "DELETE") and "application/json" not in request.headers.get("content-type", ""):
                return JSONResponse({"detail": "JSON request required"}, status_code=415)
        response = await call_next(request)
        response.headers.update({"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff",
            "Referrer-Policy": "no-referrer", "X-Frame-Options": "DENY",
            "Content-Security-Policy": "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' blob: data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"})
        return response

    @app.exception_handler(ValidationError)
    async def invalid(request, exc):
        return JSONResponse({"detail": "Configuration contains invalid values"}, status_code=422)

    @app.exception_handler(RequestValidationError)
    async def invalid_request(request, exc):
        return JSONResponse({"detail": "Configuration contains invalid values"}, status_code=422)

    @app.get("/")
    def index():
        response = FileResponse(ASSETS / "index.html")
        response.set_cookie("qa_dashboard", state.session, httponly=True, samesite="strict")
        return response

    @app.get("/api/projects")
    def projects():
        return {"projects": [state.public_project(key) for key in state.projects], "defaults": state.defaults(),
                "inherited_secrets": {key: bool(getattr(state.cfg, key).get_secret_value()) for key in SECRETS}}

    @app.post("/api/projects")
    def save_project(project: Project):
        try:
            with state.lock:
                return state.save(project)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from None

    @app.put("/api/projects/{project_id}")
    def update_project(project_id: str, project: Project):
        if project_id not in state.projects:
            raise HTTPException(404, "Project not found")
        try:
            with state.lock:
                return state.save(project, project_id)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from None

    @app.post("/api/projects/{project_id}/validate")
    def validate(project_id: str):
        if project_id not in state.projects:
            raise HTTPException(404, "Project not found")
        try:
            state.preflight(project_id)
            return {"status": "READY", "message": "Website, API, source version and database connections verified"}
        except Exception as exc:
            message = str(exc) if isinstance(exc, ValueError) else "Connection verification failed. Check the target services and credentials."
            raise HTTPException(400, message) from None

    @app.post("/api/projects/{project_id}/start")
    def start(project_id: str):
        if project_id not in state.projects:
            raise HTTPException(404, "Project not found")
        try:
            return state.start(project_id)
        except Exception as exc:
            message = str(exc) if isinstance(exc, ValueError) else "Run could not start. Verify the project connections."
            raise HTTPException(409 if state.running() else 400, message) from None

    @app.post("/api/stop")
    def stop():
        return state.stop()

    @app.get("/api/runs")
    def runs():
        ids = {p.name for p in state.cfg.runs_dir.glob("run_*") if re.fullmatch(r"run_[a-f0-9]{12}", p.name)}
        if state.current_id:
            ids.add(state.current_id)
        result = []
        for run_id in ids:
            folder = state.folder(run_id)
            report = read_json(folder / "report.json", {})
            control = read_json(state.root / run_id / "control.json", {})
            board = read_json(folder / "shared_state.json", {})
            result.append({"run_id": run_id, "project_name": control.get("project_name", "QA run"),
                "mode": report.get("execution_mode", board.get("mode", "llm")),
                "report_available": bool(report), "started_at": board.get("started_at", ""),
                "active": run_id == state.current_id and state.running(),
                "counts": {outcome: sum(c["outcome"] == outcome for c in report.get("assertions", []))
                    for outcome in ("PASS", "FAIL", "BLOCKED", "INCONCLUSIVE")}})
        return {"runs": sorted(result, key=lambda item: item["started_at"], reverse=True),
                "current_run_id": state.current_id if state.running() else None}

    @app.get("/api/runs/{run_id}")
    def snapshot(run_id: str):
        return state.snapshot(run_id)

    @app.get("/api/runs/{run_id}/events")
    async def events(run_id: str, request: Request):
        state.folder(run_id)
        async def generate():
            while not await request.is_disconnected():
                snapshot = await asyncio.to_thread(state.snapshot, run_id)
                yield "event: snapshot\ndata: " + json.dumps(snapshot) + "\n\n"
                if snapshot["status"] not in ("STARTING", "RUNNING", "STOPPING"):
                    return
                await asyncio.sleep(1)
        return StreamingResponse(generate(), media_type="text/event-stream")

    @app.get("/api/runs/{run_id}/frame")
    def frame(run_id: str):
        state.folder(run_id)
        path = state.root / run_id / "frame.jpg"
        if not path.is_file():
            raise HTTPException(404, "Waiting for browser")
        return FileResponse(path, media_type="image/jpeg")

    @app.get("/api/runs/{run_id}/report")
    def report(run_id: str):
        folder = state.folder(run_id)
        data = read_json(folder / "report.json")
        if not data:
            raise HTTPException(404, "Report is available when the run finishes")
        refs = [str(path.relative_to(folder)).replace("\\", "/") for sub in ("screenshots", "api_evidence", "db_evidence", "source_evidence")
                for path in (folder / sub).glob("*") if path.is_file()]
        return {"report": data, "evidence": refs}

    @app.get("/api/runs/{run_id}/artifacts/{artifact:path}")
    def artifact(run_id: str, artifact: str):
        folder = state.folder(run_id).resolve()
        path = (folder / artifact).resolve()
        if not path.is_relative_to(folder) or not path.is_file() or path.suffix not in (".json", ".jsonl", ".png", ".md"):
            raise HTTPException(404, "Evidence not found")
        # No HTML/SVG execution or arbitrary source/credential file access.
        return FileResponse(path)

    app.mount("/assets", StaticFiles(directory=ASSETS), name="assets")
    return app
