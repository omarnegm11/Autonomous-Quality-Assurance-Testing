"""Opt-in tests require local PostgreSQL and installed Chromium; no LLM credentials."""
import json
import os
import subprocess
import sys
import time
import socket
from uuid import uuid4
from pathlib import Path
import httpx
import pytest
from sqlalchemy import create_engine, text
from qa_poc.cli import evaluate
from qa_poc.cli import run_one
from qa_poc.builds import build
from qa_poc.config import Settings
from qa_poc.schemas import Decision
from qa_poc.schemas import Blackboard
from qa_poc.storage import Store
from qa_poc.tools import ToolLayer

pytestmark = pytest.mark.skipif(os.getenv("QA_INTEGRATION") != "1", reason="Set QA_INTEGRATION=1 after local setup")


@pytest.mark.skipif(os.name != "nt", reason="Dashboard uses Windows DPAPI for saved credentials")
def test_dashboard_worker_stream_and_four_role_authentication_report(live_app, tmp_path):
    from fastapi.testclient import TestClient
    from qa_poc.dashboard import create_app
    cfg = live_app
    with TestClient(create_app(cfg, tmp_path / "dashboard")) as client:
        client.get("/")
        payload = {"name": "Dashboard integration", "app_url": cfg.app_url, "api_url": cfg.app_url,
                   "api_spec_url": cfg.app_url + "/openapi.json", "app_source": str(cfg.app_source),
                   "workflow": "authentication", "mode": "baseline", "browser_headless": True}
        response = client.post("/api/projects", json=payload)
        assert response.status_code == 200
        project = response.json()["id"]
        assert client.post(f"/api/projects/{project}/validate", json={}).status_code == 200
        response = client.post(f"/api/projects/{project}/start", json={})
        assert response.status_code == 200
        run_id = response.json()["run_id"]
        assert client.post(f"/api/projects/{project}/start", json={}).status_code == 409
        deadline = time.monotonic() + 100
        while time.monotonic() < deadline:
            snapshot = client.get(f"/api/runs/{run_id}").json()
            if snapshot["status"] not in ("STARTING", "RUNNING", "STOPPING"):
                break
            time.sleep(.2)
        else:
            pytest.fail("Dashboard worker did not finish")
        data = client.get(f"/api/runs/{run_id}/report").json()["report"]
        assert {check["rule_id"] for check in data["assertions"]} == {"LOGIN_PROTECTED", "LOGOUT_REVOKES_SESSION"}
        assert all(check["outcome"] == "PASS" for check in data["assertions"])
        assert all(agent["executed_actions"] > 0 for agent in data["agent_activity"].values())
        assert data["cleanup"]["outcome"] == "PASS"
        frame = client.get(f"/api/runs/{run_id}/frame")
        assert frame.status_code == 200 and frame.content.startswith(b"\xff\xd8")
        assert snapshot["frame"]["captured_at"] > 0


@pytest.mark.skipif(os.name != "nt", reason="Dashboard uses Windows DPAPI for saved credentials")
def test_dashboard_stop_cleans_up_a_created_record(live_app, tmp_path):
    from fastapi.testclient import TestClient
    from qa_poc.dashboard import create_app
    cfg = live_app
    with TestClient(create_app(cfg, tmp_path / "dashboard")) as client:
        client.get("/")
        project = client.post("/api/projects", json={"name": "Stop integration", "app_url": cfg.app_url,
            "api_url": cfg.app_url, "api_spec_url": cfg.app_url + "/openapi.json", "app_source": str(cfg.app_source),
            "mode": "baseline", "browser_headless": True}).json()["id"]
        run_id = client.post(f"/api/projects/{project}/start", json={}).json()["run_id"]
        deadline = time.monotonic() + 100
        while time.monotonic() < deadline:
            snapshot = client.get(f"/api/runs/{run_id}").json()
            if snapshot["budgets"].get("actions", 0) >= 4:
                break
            time.sleep(.1)
        client.post("/api/stop", json={})
        while time.monotonic() < deadline:
            snapshot = client.get(f"/api/runs/{run_id}").json()
            if snapshot["status"] not in ("STARTING", "RUNNING", "STOPPING"):
                break
            time.sleep(.2)
        result = client.get(f"/api/runs/{run_id}/report").json()["report"]
        assert "Stopped by user" in result["stop_reason"]
        assert result["cleanup"]["outcome"] == "PASS"
        assert result["cleanup"]["records"]
        assert all(item["verified_absent"] for item in result["cleanup"]["records"])


def free_app_url():
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return f"http://127.0.0.1:{listener.getsockname()[1]}"


def test_real_three_build_comparison(tmp_path):
    cfg = Settings(runs_dir=tmp_path, app_url=free_app_url(), browser_headless=True)
    summary = evaluate(cfg, "baseline", 1)
    cases = {r["case"]: r for r in summary["results"]}
    assert summary["actual_llm_execution"] == "PENDING_CREDENTIALS"
    assert not cases["clean"]["failures"]
    assert all(o == "PASS" for o in cases["clean"]["assertions"].values())
    assert set(cases["visibility"]["failures"]) == {"TASK_PERSISTENCE", "TASK_EDIT"}
    assert cases["ownership"]["failures"] == ["OWNER_ONLY_EDIT"]
    for case in cases.values():
        assert case["cleanup"] == "PASS"
        folder = Path(case["report"]).parent
        data = json.loads((folder / "report.json").read_text())
        assert data["source_verified"]
        for bug in data["bug_candidates"]:
            assert bug["behavioral_status"] == "CONFIRMED"
            source = bug["diagnosis"]["source"]
            assert source["lines"] and source["file"] in ("tasks.js", "authorization.py")
        decisions = [json.loads(line) for line in (folder / "tool_events.jsonl").read_text().splitlines()]
        creation = next(m for m in json.loads((folder / "shared_state.json").read_text())["messages"] if m["event"] == "task_created")
        task_id = creation["record"]["id"]
        assert any(e["proposal"]["agent"] == "auth_db_explorer" and e["proposal"]["arguments"].get("task_id") == task_id for e in decisions)
        # No raw configured credentials in artifacts.
        for artifact in folder.rglob("*"):
            if artifact.suffix in (".json", ".jsonl", ".md"):
                content = artifact.read_text(encoding="utf-8")
                for secret in (cfg.test_user_a_password, cfg.test_user_b_password):
                    assert secret.get_secret_value() not in content
    engine = create_engine(cfg.verifier_database_url.get_secret_value())
    with engine.connect() as conn:
        for case in cases.values():
            assert conn.execute(text("SELECT count(*) FROM tasks WHERE run_id=:run"), {"run": case["run_id"]}).scalar_one() == 0
        with pytest.raises(Exception):
            conn.execute(text("DELETE FROM tasks WHERE false"))
    engine.dispose()


@pytest.fixture
def live_app(tmp_path):
    cfg = Settings(runs_dir=tmp_path / "runs", app_url=free_app_url(), browser_headless=True)
    source = build("clean", tmp_path / "source")
    env = {**os.environ, "APP_SOURCE": str(source), "APP_URL": cfg.app_url}
    with (tmp_path / "server.log").open("w") as output:
        process = subprocess.Popen([sys.executable, "-m", "uvicorn", "qa_poc.server:create_app", "--factory", "--port", str(httpx.URL(cfg.app_url).port)],
            env=env, stdout=output, stderr=output, creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        try:
            for _ in range(100):
                if process.poll() is not None:
                    pytest.fail("Test backend failed to start")
                try:
                    if httpx.get(cfg.app_url + "/version", timeout=1).status_code == 200:
                        break
                except httpx.TransportError:
                    pass
                time.sleep(0.2)
            else:
                pytest.fail("Test backend startup timed out")
            yield cfg.model_copy(update={"app_source": source})
        finally:
            process.terminate()
            process.wait(timeout=10)


def test_budget_cleanup_preserves_unrelated_record(live_app):
    cfg = live_app
    with httpx.Client(base_url=cfg.app_url, headers={"X-QA-Run": "sentinel_" + uuid4().hex}) as client:
        client.post("/api/login", json={"username": "user_a", "password": cfg.password("user_a")}).raise_for_status()
        sentinel = client.post("/api/tasks", json={"title": "Unrelated record must survive"}).json()
        try:
            result, folder = run_one(cfg.model_copy(update={"max_actions": 4}), "baseline")
            assert result["cleanup"]["outcome"] == "PASS"
            assert result["cleanup"]["records"]
            assert all(r["verified_absent"] for r in result["cleanup"]["records"])
            assert all(c["outcome"] == "INCONCLUSIVE" for c in result["assertions"])
            assert not result["bug_candidates"]
            assert client.get(f"/api/tasks/{sentinel['id']}").status_code == 200
        finally:
            client.delete(f"/api/tasks/{sentinel['id']}")
            client.post("/api/logout")


def test_interrupt_releases_reservations_and_cleans_created_record(live_app, monkeypatch):
    def interrupted(tools):
        for tool, arguments in (("select_test_identity", {"identity": "user_a"}),
                                ("fill_field", {"testid": "new-title", "value": "Interrupt cleanup fixture"}),
                                ("click_element", {"testid": "create"})):
            result = tools.execute(Decision(agent="ui_explorer", goal_id="INTERRUPT_FIXTURE", tool=tool,
                arguments=arguments, reason="Real-tool cancellation test fixture"))
            assert result.ok
        tools.board.reservations["fixture-record"] = "ui_explorer"
        raise KeyboardInterrupt

    monkeypatch.setattr("qa_poc.cli.run_baseline", interrupted)
    with pytest.raises(KeyboardInterrupt):
        run_one(live_app, "baseline")
    report_path = next(live_app.runs_dir.glob("run_*/report.json"))
    result = json.loads(report_path.read_text())
    assert result["cleanup"]["outcome"] == "PASS"
    assert result["cleanup"]["records"]
    assert result["cleanup"]["reservations_released"]
    snapshot = json.loads((report_path.parent / "shared_state.json").read_text())
    assert snapshot["reservations"] == {}


def test_visible_cursor_and_rechecking_after_owner_edit(live_app):
    cfg = live_app.model_copy(update={"browser_headless": False, "browser_slow_mo_ms": 20, "browser_type_delay_ms": 1})
    board = Blackboard(run_id="run_" + uuid4().hex[:12], mode="visible_integration_fixture")
    store = Store(cfg, board)
    tools = ToolLayer(cfg, board, store)

    def execute(role, tool, **arguments):
        result = tools.execute(Decision(agent=role, goal_id="VISIBLE_FIXTURE", tool=tool, arguments=arguments,
            reason="Real browser integration fixture; no model"))
        assert result.ok, result.observation
        return result.observation

    try:
        tools.setup()
        execute("diagnosis", "read_run_summary")
        execute("ui_explorer", "select_test_identity", identity="user_a")
        execute("ui_explorer", "fill_field", testid="new-title", value="Visible cursor fixture")
        execute("ui_explorer", "click_element", testid="create")
        task_id = next(iter(board.records))
        page = tools.page()
        assert page.locator("#__qa_live_cursor").count() == 1
        assert float(page.locator("#__qa_live_cursor").evaluate("e => parseFloat(e.style.left)")) > 12
        assert "click_element" in page.locator("#__qa_live_status").text_content()
        tools.take_screenshot()
        # Closing the viewer must restore isolated sessions without losing discoveries.
        tools.browser.close()
        restored = tools.observe_page()
        assert not restored["login_visible"]
        assert tools.browser_restarts == 1
        assert task_id in board.records
        execute("ui_explorer", "reload_page")
        execute("ui_explorer", "verify_ui_isolation", task_id=task_id)
        execute("api_explorer", "check_response_contract", task_id=task_id)
        execute("auth_db_explorer", "verify_task_record", task_id=task_id)
        execute("api_explorer", "call_allowed_endpoint", operation="probe_owner", task_id=task_id)
        execute("auth_db_explorer", "verify_ownership", task_id=task_id)
        execute("api_explorer", "call_allowed_endpoint", operation="edit", task_id=task_id, title="Owner's requested new title")
        # These repeated model-selected checks previously compared with the stale creation title.
        assert execute("api_explorer", "check_response_contract", task_id=task_id)["outcome"] == "PASS"
        assert execute("auth_db_explorer", "verify_task_record", task_id=task_id)["outcome"] == "PASS"
        assert execute("ui_explorer", "reload_page")["assertion"]["outcome"] == "PASS"
        assert execute("auth_db_explorer", "compare_saved_values", task_id=task_id)["outcome"] == "PASS"
        execute("auth_db_explorer", "inspect_session_status")
        execute("auth_db_explorer", "verify_logout")
        execute("api_explorer", "call_allowed_endpoint", operation="delete", task_id=task_id)
        execute("api_explorer", "check_lifecycle", task_id=task_id)
        review = execute("diagnosis", "submit_run_review", conclusion="The observed checks passed; no application defect was found in this bounded run.")
        assert review["completed"] and not review["observed_failures"]
        assert all(board.agent_status[role]["actions"] > 0 for role in ("ui_explorer", "api_explorer", "auth_db_explorer", "diagnosis"))
        assert not board.failures
    finally:
        cleanup = tools.cleanup()
        store.engine.dispose()
    assert cleanup["outcome"] == "PASS"
