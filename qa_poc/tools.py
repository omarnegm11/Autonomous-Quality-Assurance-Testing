"""Restricted, real browser/HTTP/database/source tools shared by both runners."""
import json
import os
import re
import time
from pathlib import Path
from typing import Literal
import httpx
from langchain_core.tools import StructuredTool
from pydantic import Field, create_model
from playwright.sync_api import sync_playwright
from sqlalchemy import create_engine, text
from .builds import digest
from .live_browser import BrowserPresenter
from .schemas import Check, Decision, Message, Outcome, RULES, ToolResult, now

Identity = Literal["user_a", "user_b"]
Role = Literal["ui_explorer", "api_explorer", "auth_db_explorer", "diagnosis"]

PERMISSIONS = {
    "ui_explorer": ["observe_page", "select_test_identity", "fill_field", "click_element", "reload_page", "take_screenshot", "verify_ui_isolation"],
    "api_explorer": ["describe_endpoint", "call_allowed_endpoint", "check_response_contract", "check_lifecycle"],
    "auth_db_explorer": ["select_test_identity", "inspect_session_status", "verify_logout", "verify_task_record", "verify_ownership", "compare_saved_values"],
    "diagnosis": ["read_run_summary", "submit_run_review", "request_replay", "read_run_evidence", "read_request_logs", "search_source", "read_source_range", "run_targeted_check", "submit_diagnosis"],
}


class BudgetExpired(RuntimeError):
    pass


class BrowserUnavailable(RuntimeError):
    pass


class ToolLayer:
    def __init__(self, cfg, board, store):
        self.cfg, self.board, self.store = cfg, board, store
        self.presenter = BrowserPresenter(cfg)
        self.started = time.monotonic()
        self.deadline = self.started + cfg.max_seconds
        self.clients = {}
        self.contexts = {}
        self.pages = {}
        self.active_identity = "user_a"
        self.last_responses = {}
        self.action_id = "setup"
        self.action_refs = []
        self.source_reads = set()
        self.source_hits = []
        self.replays = {}
        self.targeted = {}
        self.probes = {}
        self.pw = None
        self.browser = None
        self.browser_restarts = 0
        self.db = create_engine(cfg.verifier_database_url.get_secret_value())
        self.cleanup_client_ids = []

    def setup(self):
        version_headers = ({self.cfg.api_auth_header: self.cfg.api_auth_value.get_secret_value()}
                           if self.cfg.api_auth_value.get_secret_value() else {})
        version = httpx.get(self.cfg.app_url + "/version", headers=version_headers, timeout=10).json()
        self.board.app_version = version["app_version"]
        source = self.cfg.app_source.resolve()
        local = json.loads((source / "manifest.json").read_text())
        self.board.source_commit = local["source_commit"]
        self.board.source_verified = (version == local and digest(source) == local["source_digest"])
        with self.db.connect() as conn:
            read_only = conn.execute(text("SHOW default_transaction_read_only")).scalar_one()
            if read_only != "on":
                raise ValueError("Verifier role must have default_transaction_read_only=on")
            # Read actual table to establish connectivity to the deployment's database.
            conn.execute(text("SELECT id FROM tasks LIMIT 1"))
        for identity in self.board.identities:
            headers = {"X-QA-Run": self.board.run_id}
            if self.cfg.api_auth_value.get_secret_value():
                headers[self.cfg.api_auth_header] = self.cfg.api_auth_value.get_secret_value()
            client = httpx.Client(base_url=self.cfg.api_url or self.cfg.app_url, timeout=10,
                                 headers=headers, follow_redirects=False)
            self.clients[identity] = client
            response = client.post("/api/login", json={"username": identity, "password": self.cfg.password(identity)})
            response.raise_for_status()
            self.board.session_refs[identity] = f"http-session:{identity}"
        os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(self.cfg.browsers_path.resolve())
        self.pw = sync_playwright().start()
        self._start_browser(restore_sessions=False)
        self.show_status("UI explorer: ready")
        self.store.write("run_manifest.json", {"run_id": self.board.run_id, "mode": self.board.mode,
            **version, "source_verified": self.board.source_verified, "model_provider": self.cfg.llm_provider,
            "model_id": self.cfg.llm_model, "limits": {"actions": self.cfg.max_actions,
                "seconds": self.cfg.max_seconds, "model_tokens": self.cfg.max_model_tokens},
            "browser_headless": self.cfg.browser_headless, "browser_slow_mo_ms": self.cfg.browser_slow_mo_ms,
            "external_tracing": False, "started_at": self.board.started_at})

    def _start_browser(self, restore_sessions):
        if self.browser:
            try:
                self.browser.close()
            except Exception:
                pass
        self.contexts.clear()
        self.pages.clear()
        self.browser = self.pw.chromium.launch(headless=self.cfg.browser_headless, channel="chromium",
            slow_mo=0 if self.cfg.browser_headless and not self.cfg.live_stream_dir else self.cfg.browser_slow_mo_ms)
        for identity in self.board.identities:
            context = self.browser.new_context(extra_http_headers={"X-QA-Run": self.board.run_id})
            self.presenter.attach(context)
            if restore_sessions:
                cookie = self.clients[identity].cookies.get("qa_session")
                if cookie:
                    context.add_cookies([{"name": "qa_session", "value": cookie, "url": self.cfg.app_url,
                        "httpOnly": True, "sameSite": "Strict"}])
            self.contexts[identity] = context
            page = context.new_page()
            page.set_default_timeout(5000)
            page.on("response", lambda response, who=identity: self._capture(response, who))
            page.goto(self.cfg.app_url)
            self.pages[identity] = page

    def ensure_browser(self, identity=None):
        identity = identity or self.active_identity
        # Closing an inactive account window must not invalidate the active viewer.
        healthy = (self.browser is not None and self.browser.is_connected() and
                   identity in self.pages and not self.pages[identity].is_closed())
        if healthy:
            return
        if self.pw is None or self.browser_restarts >= self.cfg.browser_max_restarts:
            raise BrowserUnavailable("Browser unavailable; recovery limit reached. Other independent checks can continue.")
        self.browser_restarts += 1
        print(f"Browser closed; restoring isolated test sessions ({self.browser_restarts}/{self.cfg.browser_max_restarts})", flush=True)
        self.store.append("browser_events.jsonl", {"event": "browser_recovery", "attempt": self.browser_restarts})
        self._start_browser(restore_sessions=True)

    def _capture(self, response, identity):
        if response.request.method == "POST" and response.url.endswith("/api/tasks") and response.status == 201:
            record = response.json()
            self._discover(record, "ui_explorer")
        elif response.request.method == "PATCH" and "/api/tasks/" in response.url and response.status == 200:
            task_id = int(response.url.rsplit("/", 1)[1])
            record = self.board.records.get(task_id)
            if record and record["owner"] == identity:
                requested = response.request.post_data_json
                for field in ("title", "completed"):
                    if field in requested:
                        record[field] = requested[field]

    def _discover(self, record, sender):
        if record["run_id"] != self.board.run_id:
            raise ValueError("Record not owned by this run")
        task_id = record["id"]
        self.board.records[task_id] = dict(record)
        if task_id not in self.board.cleanup_records:
            self.board.cleanup_records.append(task_id)
        pending = ["API_VALUES_MATCH", "DB_VALUES_MATCH", "OWNER_ONLY_EDIT", "TASK_PERSISTENCE", "OWNER_UI_ISOLATION"]
        self.board.pending_checks = list(dict.fromkeys(self.board.pending_checks + pending))
        self.board.messages.append(Message(sender=sender, run_id=self.board.run_id,
            event="task_created", record=dict(record), pending_checks=pending))

    def consume(self):
        self.check_stop()
        if time.monotonic() >= self.deadline or self.board.budgets["actions"] >= self.cfg.max_actions:
            raise BudgetExpired("Tool/time budget exhausted")
        self.board.budgets["actions"] += 1

    def check_stop(self):
        if getattr(self.cfg, "stop_file", None) and self.cfg.stop_file.exists():
            raise BudgetExpired("Stopped by user; cleanup performed")

    def remaining(self):
        return max(0.1, min(10, self.deadline - time.monotonic()))

    def owned(self, task_id):
        if task_id not in self.board.cleanup_records:
            raise ValueError("Only this run's synthetic records are permitted")
        return self.board.records[task_id]

    def evidence(self, category, value):
        name = f"{category}/{self.action_id}_{len(self.action_refs):02d}.json"
        self.store.write(name, value)
        self.action_refs.append(name)
        return name

    def check(self, rule_id, passed, expected, actual):
        check = Check(rule_id=rule_id, workflow_id=RULES[rule_id][0], outcome=Outcome.PASS if passed else Outcome.FAIL,
            expected=expected, actual=actual, evidence_refs=self.action_refs.copy(), action_id=self.action_id)
        # Preserve the first observed failure rather than erasing it with a later pass.
        if rule_id not in self.board.assertions or self.board.assertions[rule_id].outcome != Outcome.FAIL:
            self.board.assertions[rule_id] = check
        if not passed and rule_id not in self.board.failures:
            self.board.failures.append(rule_id)
        if rule_id not in self.board.coverage:
            self.board.coverage.append(rule_id)
        self.board.pending_checks = [p for p in self.board.pending_checks if p != rule_id]
        return check.model_dump(mode="json")

    def registry(self, role, with_reason=False):
        registered = [StructuredTool.from_function(getattr(self, name)) for name in PERMISSIONS[role]]
        if with_reason:
            for tool in registered:
                tool.args_schema = create_model(f"{tool.name}Proposal", __base__=tool.args_schema,
                    action_reason=(str, Field(description="Brief action rationale based on observed facts, without private reasoning")))
        return registered

    def execute(self, proposal: Decision, tool_call_id=None):
        self.consume()
        self.action_id = f"action_{len(self.board.action_history) + 1:04d}"
        self.action_refs = []
        identity = proposal.arguments.get("identity", self.active_identity)
        key = f"record:{proposal.arguments['task_id']}" if "task_id" in proposal.arguments else f"session:{identity}"
        current = self.board.reservations.get(key)
        if current and current != proposal.agent:
            raise ValueError("Resource reserved by another specialist")
        self.board.reservations[key] = proposal.agent
        try:
            if proposal.tool not in PERMISSIONS[proposal.agent]:
                raise ValueError("Tool not permitted for this role")
            tool = next(t for t in self.registry(proposal.agent) if t.name == proposal.tool)
            self.show_status(f"{proposal.agent}: {proposal.tool}")
            # Validate unknown fields too: StructuredTool otherwise silently ignores extras.
            args = tool.args_schema.model_validate(proposal.arguments)
            if set(proposal.arguments) - set(tool.args_schema.model_fields):
                raise ValueError("Unknown tool arguments")
            observation = getattr(self, proposal.tool)(**args.model_dump())
            result = ToolResult(ok=True, action_id=self.action_id, observation=observation,
                                evidence_refs=self.action_refs.copy())
        except BudgetExpired:
            raise
        except Exception as exc:
            # No automatic mutation retry: a timed-out POST may already have committed.
            result = ToolResult(ok=False, action_id=self.action_id,
                observation={"error_type": type(exc).__name__, "error": str(exc)[:1000],
                             "outcome": "INCONCLUSIVE", "retry_policy": "Only explicitly retry safe reads once"},
                evidence_refs=self.action_refs.copy())
        finally:
            self.board.reservations.pop(key, None)
        event = {"proposal": proposal.model_dump(), "tool_call_id": tool_call_id,
                 "result": result.model_dump(mode="json")}
        self.board.action_history.append(event)
        status = self.board.agent_status.setdefault(proposal.agent, {})
        status.update({"status": "WAITING", "last_tool": proposal.tool,
            "actions": status.get("actions", 0) + 1, "last_action_ok": result.ok})
        self.board.observations.append({"timestamp": now(), "agent": proposal.agent, **result.model_dump()})
        self.store.append("tool_events.jsonl", event)
        self.store.save()
        assertion = result.observation.get("assertion", result.observation)
        status = assertion.get("outcome", "executed" if result.ok else "INCONCLUSIVE")
        print(f"{self.action_id} {proposal.agent}: {proposal.tool} -> {status}", flush=True)
        self.show_status(f"{proposal.agent}: {proposal.tool} -> {status}")
        return result

    def page(self):
        self.ensure_browser()
        return self.pages[self.active_identity]

    def show_status(self, status):
        if self.pages:
            try:
                labels = {"ui_explorer": "UI", "api_explorer": "API", "auth_db_explorer": "Auth/DB", "diagnosis": "Diagnosis"}
                activity = " | ".join(f"{label}: {self.board.agent_status.get(role, {}).get('actions', 0)} actions"
                    for role, label in labels.items())
                self.presenter.focus(self.page(), f"{self.active_identity} | {status}\n{activity}")
            except Exception:
                # Presentation failure must not abort an independent API/DB/model action.
                pass

    def _observe(self, page):
        page.wait_for_function("document.body.dataset.ready === 'true'")
        return page.evaluate("""() => ({url: location.href, login_visible: !document.getElementById('login-panel').hidden,
          message: document.getElementById('message').textContent,
          fields: [...document.querySelectorAll('input:not([type=password])')].filter(e => e.checkVisibility()).map(e => ({testid:e.dataset.testid,value:e.value})),
          buttons: [...document.querySelectorAll('button')].filter(e => e.checkVisibility()).map(e => ({testid:e.dataset.testid,text:e.textContent})),
          visible_tasks: [...document.querySelectorAll('[data-task-id]')].map(e => ({id:Number(e.dataset.taskId), title:e.querySelector('input').value}))})""")

    def observe_page(self) -> dict:
        """Observe the active browser's URL, visible tasks, form values and permitted selectors."""
        return {"identity": self.active_identity, **self._observe(self.page())}

    def select_test_identity(self, identity: Identity) -> dict:
        """Switch to an isolated test browser context and sign in if necessary; secrets stay in tool layer."""
        self.active_identity = identity
        page = self.page()
        self.presenter.focus(page, f"{identity} | UI explorer: select identity")
        if self._observe(page)["login_visible"]:
            self.presenter.fill(page, page.get_by_test_id("username"), identity)
            self.presenter.fill(page, page.get_by_test_id("password"), self.cfg.password(identity))
            with page.expect_response(lambda r: r.url.endswith("/api/tasks") and r.request.method == "GET"):
                self.presenter.click(page, page.get_by_test_id("login"))
            page.locator("#task-panel").wait_for(state="visible")
        return self.observe_page()

    def fill_field(self, testid: str, value: str) -> dict:
        """Fill new-title or title-ID from the current observation. Password fields are excluded."""
        if testid == "new-title":
            if len(value) > 200:
                raise ValueError("Title too long")
        elif re.fullmatch(r"title-\d+", testid):
            self.owned(int(testid.split("-")[1]))
        else:
            raise ValueError("Field not permitted")
        self.presenter.fill(self.page(), self.page().get_by_test_id(testid), value)
        return self.observe_page()

    def click_element(self, testid: str) -> dict:
        """Click create, logout, or save/complete/delete-ID controls observed on the active page."""
        if testid not in ("create", "logout"):
            if not re.fullmatch(r"(?:save|complete|delete)-\d+", testid):
                raise ValueError("Control not permitted")
            task_id = int(testid.split("-")[1])
            self.owned(task_id)
            if testid.startswith("delete-"):
                self._may_delete()
        page = self.page()
        if testid == "create" and not page.get_by_test_id("new-title").input_value().strip():
            self.presenter.click(page, page.get_by_test_id(testid))
            return {**self.observe_page(), "validation": "Task title is required"}
        with page.expect_response(lambda r: r.url.endswith("/api/tasks") and r.request.method == "GET"):
            self.presenter.click(page, page.get_by_test_id(testid))
        return self.observe_page()

    def reload_page(self) -> dict:
        """Reload and compare run-owned incomplete tasks with the visible task list."""
        page = self.page()
        page.reload()
        observation = self.observe_page()
        if observation["login_visible"]:
            return observation
        expected = [r for r in self.board.records.values() if r["owner"] == self.active_identity and not r.get("deleted") and not r["completed"]]
        if expected:
            screenshot = self.take_screenshot()["path"]
            visible = {r["id"]: r["title"] for r in observation["visible_tasks"]}
            self.evidence("api_evidence", {"browser_after_reload": observation, "screenshot": screenshot})
            observation["assertion"] = self.check("TASK_PERSISTENCE", all(visible.get(r["id"]) == r["title"] for r in expected),
                {r["id"]: r["title"] for r in expected}, visible)
        return observation

    def take_screenshot(self) -> dict:
        """Capture the current page after hiding credential inputs."""
        name = f"screenshots/{self.action_id}_{len(self.action_refs)}.png"
        self.page().screenshot(path=str(self.store.folder / name), full_page=True,
                               mask=[self.page().locator("#password")])
        self.action_refs.append(name)
        return {"path": name}

    def verify_ui_isolation(self, task_id: int) -> dict:
        """Verify B's isolated browser cannot see A's task or its edit control."""
        self.owned(task_id)
        previous = self.active_identity
        try:
            self.select_test_identity("user_b")
            self.page().reload()
            observation = self.observe_page()
            visible = [r["id"] for r in observation["visible_tasks"]]
            self.take_screenshot()
            return self.check("OWNER_UI_ISOLATION", task_id not in visible and not observation["login_visible"],
                              "A's task absent from B's UI", observation)
        finally:
            self.active_identity = previous
            self.show_status("UI isolation check finished")

    def _request(self, identity, method, path, body=None, client=None):
        # Every real request, including replay substeps, consumes the total budget.
        self.consume()
        for attempt in range(2 if method == "GET" else 1):
            try:
                response = (client or self.clients[identity]).request(method, path, json=body, timeout=self.remaining())
                break
            except httpx.TransportError as exc:
                self.evidence("api_evidence", {"method": method, "path": path, "attempt": attempt + 1,
                    "outcome": "INCONCLUSIVE", "error_type": type(exc).__name__})
                if method != "GET" or attempt:
                    raise
                self.consume()
        result = {"identity": identity, "method": method, "path": path, "status": response.status_code,
                  "body": response.json() if response.content else None,
                  "request_id": response.headers.get("X-Request-ID")}
        self.evidence("api_evidence", result)
        return result

    def _record(self, task_id):
        self.owned(task_id)
        with self.db.connect() as conn:
            row = conn.execute(text("SELECT id,owner,title,completed,run_id FROM tasks WHERE id=:id AND run_id=:run"),
                               {"id": task_id, "run": self.board.run_id}).mappings().first()
        result = dict(row) if row else None
        self.evidence("db_evidence", {"task_id": task_id, "row": result})
        return result

    def describe_endpoint(self) -> dict:
        """Read real OpenAPI schemas and the allowed operation/status contract."""
        headers = ({self.cfg.api_auth_header: self.cfg.api_auth_value.get_secret_value()}
                   if self.cfg.api_auth_value.get_secret_value() else {})
        document = httpx.get(self.cfg.api_spec_url or (self.cfg.api_url or self.cfg.app_url) + "/openapi.json",
                             headers=headers, timeout=self.remaining()).json()
        self.evidence("api_evidence", {"openapi": document})
        paths = {path: {method: {"summary": spec.get("summary"), "status_codes": list(spec.get("responses", {}))}
            for method, spec in methods.items()} for path, methods in document["paths"].items() if path.startswith("/api/")}
        return {"paths": paths, "operations": {
            "get": "GET /api/tasks/{id}: owner 200, other 403, missing 404",
            "probe_owner": "B PATCH A's task: 403; DB must stay unchanged",
            "edit": "A PATCH own task: 200", "delete": "A DELETE own task: 204"}}

    def _may_delete(self):
        required = {"TASK_PERSISTENCE", "API_VALUES_MATCH", "DB_VALUES_MATCH", "OWNER_ONLY_EDIT", "OWNER_UI_ISOLATION", "TASK_EDIT"}
        if not required.issubset(self.board.assertions):
            raise ValueError("Deletion held until dependent UI/API/DB/ownership/edit checks finish")
        if any(rule not in self.board.diagnoses for rule in self.board.failures):
            raise ValueError("Deletion held until observed failures have been investigated")

    def call_allowed_endpoint(self, operation: Literal["get", "probe_owner", "edit", "delete"],
                              task_id: int, title: str = "") -> dict:
        """Execute one allowlisted API operation on a real run-owned task; probe_owner uses B's independent session."""
        record = self.owned(task_id)
        path = f"/api/tasks/{task_id}"
        if operation == "get":
            result = self._request(record["owner"], "GET", path)
        elif operation == "probe_owner":
            if not {"API_VALUES_MATCH", "DB_VALUES_MATCH"}.issubset(self.board.assertions):
                raise ValueError("Complete initial API/DB comparisons before the ownership mutation probe")
            if record["owner"] != "user_a":
                raise ValueError("Ownership probe requires A's record")
            before = self._record(task_id)
            result = self._request("user_b", "PATCH", path, {"title": record["title"] + " [B probe]"})
            self.probes[task_id] = {"before": before, "response": result}
            self.board.pending_checks.append("OWNER_ONLY_EDIT")
        elif operation == "edit":
            if "OWNER_ONLY_EDIT" not in self.board.assertions:
                raise ValueError("Finish ownership verification before changing baseline values")
            if not title or len(title) > 200:
                raise ValueError("A valid new title is required")
            result = self._request(record["owner"], "PATCH", path, {"title": title})
            self.last_responses["edit"] = result
            record["edited_title"] = title
            if result["status"] == 200:
                # Expected current state follows the owner's requested mutation,
                # never the returned title (which could itself be defective).
                record["title"] = title
        else:
            self._may_delete()
            result = self._request(record["owner"], "DELETE", path)
            self.last_responses["delete"] = result
        self.last_responses[operation] = result
        return result

    def check_response_contract(self, task_id: int) -> dict:
        """Fetch the discovered task and assert exact HTTP status, fields, saved values and identity."""
        expected = self.owned(task_id)
        response = self._request(expected["owner"], "GET", f"/api/tasks/{task_id}")
        keys = ("id", "title", "owner", "completed", "run_id")
        passed = response["status"] == 200 and all(response["body"].get(k) == expected[k] for k in keys)
        return self.check("API_VALUES_MATCH", passed, {k: expected[k] for k in keys}, response)

    def verify_task_record(self, task_id: int) -> dict:
        """SELECT this run's task using parameterized read-only SQL; compare saved values."""
        expected = self.owned(task_id)
        row = self._record(task_id)
        keys = ("id", "title", "owner", "completed", "run_id")
        return self.check("DB_VALUES_MATCH", row is not None and all(row[k] == expected[k] for k in keys),
                          {k: expected[k] for k in keys}, row)

    def verify_ownership(self, task_id: int) -> dict:
        """Consume the API specialist's B-edit probe and compare actual stored values before and after."""
        self.owned(task_id)
        if task_id not in self.probes:
            raise ValueError("API explorer must perform probe_owner first")
        probe = self.probes[task_id]
        after = self._record(task_id)
        actual = {**probe, "after": after}
        return self.check("OWNER_ONLY_EDIT", probe["response"]["status"] == 403 and after == probe["before"],
                          {"status": 403, "row": probe["before"]}, actual)

    def compare_saved_values(self, task_id: int) -> dict:
        """Compare the API, DB and current UI with the owner's requested edit."""
        record = self.owned(task_id)
        if "edited_title" not in record:
            raise ValueError("Owner edit must be performed first")
        response = self._request(record["owner"], "GET", f"/api/tasks/{task_id}")
        row = self._record(task_id)
        self.ensure_browser(record["owner"])
        page = self.pages[record["owner"]]
        page.reload()
        observation = self._observe(page)
        visible = {t["id"]: t["title"] for t in observation["visible_tasks"]}
        title = record["edited_title"]
        actual = {"edit_response": self.last_responses.get("edit"), "get": response, "db": row, "ui": observation}
        passed = (self.last_responses.get("edit", {}).get("status") == 200 and response["status"] == 200
                  and response["body"]["title"] == title and row is not None and row["title"] == title and visible.get(task_id) == title)
        return self.check("TASK_EDIT", passed, {"status": 200, "saved_title_in_api_db_ui": title}, actual)

    def check_lifecycle(self, task_id: int) -> dict:
        """After deletion assert 204, API 404, DB absence and UI absence."""
        record = self.owned(task_id)
        if "delete" not in self.last_responses:
            raise ValueError("Delete must be performed first")
        response = self._request(record["owner"], "GET", f"/api/tasks/{task_id}")
        row = self._record(task_id)
        self.ensure_browser(record["owner"])
        page = self.pages[record["owner"]]
        page.reload()
        observation = self._observe(page)
        passed = (self.last_responses["delete"]["status"] == 204 and response["status"] == 404
                  and row is None and task_id not in [t["id"] for t in observation["visible_tasks"]])
        record["deleted"] = row is None
        return self.check("TASK_DELETE", passed, {"delete_status": 204, "get_status": 404, "row": None, "visible": False},
                          {"delete": self.last_responses["delete"], "get": response, "row": row, "ui": observation})

    def inspect_session_status(self, identity: Identity = "user_a") -> dict:
        """Verify an independent valid test session reaches the protected endpoint with exactly 200."""
        response = self._request(identity, "GET", "/api/tasks")
        return self.check("LOGIN_PROTECTED", response["status"] == 200, 200, response)

    def verify_logout(self, identity: Identity = "user_a") -> dict:
        """Use a disposable session, logout, then replay its OLD cookie to verify revocation (401)."""
        headers = ({self.cfg.api_auth_header: self.cfg.api_auth_value.get_secret_value()}
                   if self.cfg.api_auth_value.get_secret_value() else {})
        client = httpx.Client(base_url=self.cfg.api_url or self.cfg.app_url, timeout=self.remaining(), headers=headers)
        try:
            self.consume()
            client.post("/api/login", json={"username": identity, "password": self.cfg.password(identity)}).raise_for_status()
            old_cookie = client.cookies.get("qa_session")
            before = self._request(identity, "GET", "/api/tasks", client=client)
            logout = self._request(identity, "POST", "/api/logout", client=client)
            client.cookies.set("qa_session", old_cookie)
            after = self._request(identity, "GET", "/api/tasks", client=client)
            return self.check("LOGOUT_REVOKES_SESSION", before["status"] == 200 and logout["status"] == 204 and after["status"] == 401,
                              {"before": 200, "logout": 204, "old_session": 401}, {"before": before, "logout": logout, "after": after})
        finally:
            client.close()

    def read_run_summary(self) -> dict:
        """Inspect current real checks, source matching and specialist actions, even when no application defect was found."""
        self.board.agent_reviews.setdefault("diagnosis", {})["overview_read"] = True
        return {"run_id": self.board.run_id, "source_verified": self.board.source_verified,
            "app_version": self.board.app_version, "source_commit": self.board.source_commit,
            "checks": {k: v.outcome.value for k, v in self.board.assertions.items()},
            "untested": [r for r in self.board.target_rules if r not in self.board.assertions], "failures": self.board.failures,
            "agent_activity": {role: sum(e["proposal"]["agent"] == role for e in self.board.action_history)
                for role in PERMISSIONS}, "available_failure_tools": "Replay and source diagnosis only for actual failures"}

    def submit_run_review(self, conclusion: str) -> dict:
        """Finalize an evidence review after checks finish. Passing runs receive no invented defect or root cause."""
        if not self.board.agent_reviews.get("diagnosis", {}).get("overview_read"):
            raise ValueError("Read the run summary before reviewing")
        if any(rule not in self.board.assertions for rule in self.board.target_rules):
            raise ValueError("Target checks have not all finished yet")
        if any(rule not in self.board.diagnoses for rule in self.board.failures):
            raise ValueError("Investigate observed failures before final review")
        review = {"overview_read": True, "completed": True, "conclusion": conclusion,
            "checks": {k: v.outcome.value for k, v in self.board.assertions.items()},
            "observed_failures": list(self.board.failures), "source_verified": self.board.source_verified,
            "confirmed_bug_count": sum(d.get("behavioral_status") == "CONFIRMED" for d in self.board.diagnoses.values())}
        self.board.agent_reviews["diagnosis"] = review
        self.evidence("source_evidence", {"run_review": review})
        return review

    def read_run_evidence(self, rule_id: str) -> dict:
        """Read an observed failure with its actual history and evidence references."""
        if rule_id not in self.board.failures:
            raise ValueError("No such observed failure")
        return {"failure": self.board.assertions[rule_id].model_dump(mode="json"),
                "records": self.board.records, "history": [{"agent": e["proposal"]["agent"],
                    "tool": e["proposal"]["tool"], "arguments": e["proposal"]["arguments"],
                    "action_id": e["result"]["action_id"], "ok": e["result"]["ok"],
                    "evidence_refs": e["result"]["evidence_refs"]} for e in self.board.action_history[-12:]],
                "source_verified": self.board.source_verified}

    def _fresh_task(self, title):
        result = self._request("user_a", "POST", "/api/tasks", {"title": title})
        if result["status"] != 201:
            raise ValueError("Replay setup failed")
        record = result["body"]
        # Replay records belong to cleanup, but do not replace explorer expectations.
        self.board.cleanup_records.append(record["id"])
        self.board.records[record["id"]] = record
        return record

    def request_replay(self, rule_id: str) -> dict:
        """Replay a supported failed assertion in an isolated fresh context and fresh run-owned record; max two attempts."""
        if rule_id not in self.board.failures:
            raise ValueError("Replay requires an observed failure")
        attempts = self.replays.setdefault(rule_id, [])
        if len(attempts) >= 2:
            raise ValueError("Replay limit reached")
        self.consume()
        record = None
        task_id = None
        steps = []
        self.ensure_browser()
        context = self.browser.new_context(extra_http_headers={"X-QA-Run": self.board.run_id})
        self.presenter.attach(context)
        try:
            if rule_id == "OWNER_ONLY_EDIT":
                record = self._fresh_task(f"{self.board.run_id} replay {len(attempts) + 1}")
                task_id = record["id"]
                before = self._record(task_id)
                response = self._request("user_b", "PATCH", f"/api/tasks/{task_id}", {"title": record["title"] + " [B probe]"})
                after = self._record(task_id)
                reproduced = response["status"] != 403 or before != after
                steps = ["Login A and B in independent sessions", "A creates an incomplete task", "B PATCHes A's actual returned ID", "Compare HTTP status and stored values"]
                details = {"before": before, "response": response, "after": after}
            elif rule_id in ("TASK_PERSISTENCE", "TASK_EDIT"):
                # New browser session, equivalent authenticated starting state, no shared mutable page.
                page = context.new_page()
                page.goto(self.cfg.app_url)
                self.presenter.focus(page, "Diagnosis: replay in an isolated session")
                self.presenter.fill(page, page.get_by_test_id("username"), "user_a")
                self.presenter.fill(page, page.get_by_test_id("password"), self.cfg.password("user_a"))
                with page.expect_response(lambda r: r.url.endswith("/api/tasks") and r.request.method == "GET"):
                    self.presenter.click(page, page.get_by_test_id("login"))
                page.locator("#task-panel").wait_for(state="visible")
                self.presenter.fill(page, page.get_by_test_id("new-title"), f"{self.board.run_id} replay {len(attempts) + 1}")
                self.consume()
                with page.expect_response(lambda r: r.url.endswith("/api/tasks") and r.request.method == "POST") as created:
                    self.presenter.click(page, page.get_by_test_id("create"))
                if created.value.status != 201:
                    raise ValueError("Replay UI task creation failed")
                record = created.value.json()
                task_id = record["id"]
                self.board.cleanup_records.append(task_id)
                self.board.records[task_id] = record
                if rule_id == "TASK_EDIT":
                    changed = record["title"] + " edited"
                    self._request("user_a", "PATCH", f"/api/tasks/{task_id}", {"title": changed})
                    record["title"] = changed
                page.reload()
                observation = self._observe(page)
                visible = {t["id"]: t["title"] for t in observation["visible_tasks"]}
                row = self._record(task_id)
                api = self._request("user_a", "GET", f"/api/tasks/{task_id}")
                reproduced = row is not None and api["status"] == 200 and visible.get(task_id) != record["title"]
                screenshot = f"screenshots/{self.action_id}_replay.png"
                page.screenshot(path=str(self.store.folder / screenshot), full_page=True, mask=[page.locator("#password")])
                self.action_refs.append(screenshot)
                details = {"ui": observation, "row": row, "api": api}
                steps = ["Login A in a fresh browser", "Fill task title and click Create; retain the actual returned ID"]
                if rule_id == "TASK_EDIT":
                    steps.append("A PATCHes the saved task title")
                steps += ["Reload task page", "Compare visible IDs/title with API and DB"]
            else:
                reproduced, details = False, {"outcome": "INCONCLUSIVE", "reason": "No approved replay template for this rule"}
            result = {"rule_id": rule_id, "attempt": len(attempts) + 1, "reproduced": reproduced,
                      "starting_state": "Independent authenticated contexts, newly created incomplete A-owned task",
                      "steps": steps, "details": details, "task_id": task_id}
            self.evidence("source_evidence", result)
            attempts.append(result)
            return result
        finally:
            try:
                context.request.post(self.cfg.app_url + "/api/logout", timeout=5000)
            except Exception:
                pass
            context.close()
            if task_id is not None:
                self._request("user_a", "DELETE", f"/api/tasks/{task_id}")
                record["deleted"] = True

    def _source(self, name):
        if not self.board.source_verified:
            raise ValueError("Source/deployment mismatch: code conclusions unavailable")
        root = self.cfg.app_source.resolve()
        path = (root / name).resolve()
        if not path.is_relative_to(root) or name not in ("tasks.js", "authorization.py", "backend.py", "index.html"):
            raise ValueError("Source path outside approved application files")
        if digest(root) != json.loads((root / "manifest.json").read_text())["source_digest"]:
            self.board.source_verified = False
            raise ValueError("Source changed during run")
        return path

    def search_source(self, query: str) -> dict:
        """Search matching deployed application source for a literal string; returns real path/line hits."""
        if not query or len(query) > 100:
            raise ValueError("Provide a short literal search string")
        hits = []
        for name in ("tasks.js", "authorization.py", "backend.py", "index.html"):
            path = self._source(name)
            hits.extend({"file": name, "line": i, "text": line} for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1) if query.lower() in line.lower())
        self.source_hits.extend(hits[:30])
        self.evidence("source_evidence", {"query": query, "hits": hits[:30]})
        return {"hits": hits[:30]}

    def read_source_range(self, file: str, start: int, end: int) -> dict:
        """Read actual numbered source lines from a verified build (max 80 lines, eight files)."""
        path = self._source(file)
        if len(self.source_reads | {file}) > 8 or start < 1 or end < start or end - start >= 80:
            raise ValueError("Source read limit exceeded")
        self.source_reads.add(file)
        lines = path.read_text(encoding="utf-8").splitlines()
        if start > len(lines):
            raise ValueError("Line outside file")
        result = {"file": file, "lines": [{"line": i, "text": lines[i-1]} for i in range(start, min(end, len(lines)) + 1)],
                  "source_commit": self.board.source_commit, "source_digest": digest(self.cfg.app_source.resolve())}
        self.evidence("source_evidence", result)
        return result

    def read_request_logs(self, request_id: str) -> dict:
        """Read a backend request log by an ID already present in this run's API evidence."""
        known = any(request_id in p.read_text(encoding="utf-8") for p in (self.store.folder / "api_evidence").glob("*.json"))
        if not known:
            raise ValueError("Request ID must come from this run")
        with self.db.connect() as conn:
            row = conn.execute(text("SELECT request_id,method,path,status,timestamp FROM request_logs WHERE request_id=:id"), {"id": request_id}).mappings().first()
        result = {"log": dict(row) if row else None}
        self.evidence("source_evidence", result)
        return result

    def run_targeted_check(self, rule_id: str) -> dict:
        """Test completed/incomplete UI visibility or ownership status/data on a disposable record."""
        if rule_id not in self.board.failures:
            raise ValueError("Targeted check requires an observed failure")
        if rule_id in self.targeted:
            return self.targeted[rule_id]
        if rule_id == "OWNER_ONLY_EDIT":
            result = self.request_replay(rule_id)
        else:
            record = self._fresh_task(f"{self.board.run_id} targeted")
            task_id = record["id"]
            self.ensure_browser("user_a")
            page = self.pages["user_a"]
            try:
                page.reload()
                incomplete = self._observe(page)
                self._request("user_a", "PATCH", f"/api/tasks/{task_id}", {"completed": True})
                page.reload()
                completed = self._observe(page)
                result = {"task_id": task_id,
                    "incomplete_visible": task_id in [t["id"] for t in incomplete["visible_tasks"]],
                    "completed_visible": task_id in [t["id"] for t in completed["visible_tasks"]]}
                self.evidence("source_evidence", result)
            finally:
                self._request("user_a", "DELETE", f"/api/tasks/{task_id}")
                record["deleted"] = True
        self.targeted[rule_id] = result
        return result

    def submit_diagnosis(self, rule_id: str, explanation: str, file: str = "", line: int = 0) -> dict:
        """Submit an evidence-grounded likely cause; source reference must have been read, with replay and targeted evidence."""
        if rule_id not in self.board.failures:
            raise ValueError("No observed failure")
        attempts = self.replays.get(rule_id, [])
        reproduced = any(a["reproduced"] for a in attempts)
        source = None
        if file:
            if not reproduced or rule_id not in self.targeted or file not in self.source_reads:
                raise ValueError("A likely code location requires replay, targeted check and actual source read")
            source = self.read_source_range(file, line, line)
        result = {"rule_id": rule_id, "behavioral_status": "CONFIRMED" if reproduced else "OBSERVED",
            "root_cause_status": "LIKELY" if source else "UNCONFIRMED", "explanation": explanation,
            "source": source, "reproduction_attempts": len(attempts), "replay": attempts,
            "targeted_check": self.targeted.get(rule_id), "evidence_refs": self.action_refs.copy(),
            "uncertainty": "Likely source association; no automatic fix or causal proof" if source else "Exact cause remains unconfirmed"}
        self.board.diagnoses[rule_id] = result
        return result

    def cleanup(self):
        """Budget-independent cleanup restricted by run ID and ownership, including creations whose response was lost."""
        outcomes = []
        try:
            # Reconcile via API to recover committed creations after a timeout/cancellation.
            for identity, client in self.clients.items():
                try:
                    response = client.get("/api/tasks", timeout=5)
                    response.raise_for_status()
                    for row in response.json():
                        if row["run_id"] == self.board.run_id:
                            self.board.records.setdefault(row["id"], row)
                            if row["id"] not in self.board.cleanup_records:
                                self.board.cleanup_records.append(row["id"])
                    for task_id in self.board.cleanup_records:
                        row = self.board.records[task_id]
                        if row["owner"] != identity:
                            continue
                        result = client.delete(f"/api/tasks/{task_id}", timeout=5)
                        check = client.get(f"/api/tasks/{task_id}", timeout=5)
                        outcomes.append({"task_id": task_id, "delete_status": result.status_code,
                                         "verified_absent": check.status_code == 404})
                    client.post("/api/logout", timeout=5)
                except Exception as exc:
                    outcomes.append({"identity": identity, "error_type": type(exc).__name__})
            # Revoke browser sessions as well; never persist their cookies.
            for context in self.contexts.values():
                try:
                    context.request.post(self.cfg.app_url + "/api/logout", timeout=5000)
                except Exception:
                    pass
            self.board.reservations.clear()
            result = {"outcome": "PASS" if all(o.get("verified_absent") for o in outcomes) else "INCONCLUSIVE",
                      "records": outcomes, "reservations_released": True}
            self.store.write("cleanup.json", result)
            self.store.save()
            return result
        finally:
            for client in self.clients.values():
                client.close()
            if self.browser:
                self.browser.close()
            if self.pw:
                self.pw.stop()
            self.db.dispose()
