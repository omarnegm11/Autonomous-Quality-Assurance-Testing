import json
import os
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient

from qa_poc.builds import build
from qa_poc.config import Settings
from qa_poc.dashboard import Dashboard, Project, create_app, protect
from qa_poc.schemas import Blackboard
from qa_poc.tools import BudgetExpired, ToolLayer


def settings(tmp_path):
    return Settings(_env_file=None, test_database_url="postgresql+psycopg://app:password@localhost/app",
        agent_state_database_url="postgresql+psycopg://state:password@localhost/state",
        verifier_database_url="postgresql+psycopg://reader:password@localhost/app",
        test_user_a_password="owner-secret-123", test_user_b_password="other-secret-456",
        llm_api_key="model-secret-789", runs_dir=tmp_path / "runs")


@pytest.mark.skipif(os.name != "nt", reason="Windows credential encryption")
def test_encrypted_project_roundtrip_keeps_secrets_out_of_responses(tmp_path):
    cfg = settings(tmp_path)
    app = create_app(cfg, tmp_path / "control")
    with TestClient(app) as client:
        assert client.get("/api/projects").status_code == 401
        client.get("/")
        project = client.post("/api/projects", json={"name": "Secure project", "secret_updates": {"llm_api_key": "new-secret"}}).json()
        assert "new-secret" not in json.dumps(project)
        assert project["secrets_configured"]["llm_api_key"]
        disk = (tmp_path / "control/projects.bin").read_bytes()
        assert b"new-secret" not in disk
        assert b"Secure project" not in disk
        saved_id = project["id"]
        assert client.put(f"/api/projects/{saved_id}", json={"name": "Renamed", "secret_updates": {"llm_api_key": ""}}).status_code == 200
        # The same OS account can reload it; blank fields retain saved credentials.
        loaded = Dashboard(cfg, tmp_path / "control")
        assert loaded.settings_for(saved_id)[1].llm_api_key.get_secret_value() == "new-secret"
        public = client.get("/api/projects").text
        for secret in ("new-secret", "owner-secret-123", "other-secret-456", "model-secret-789"):
            assert secret not in public
        malformed = client.post("/api/projects", json={"max_actions": "secret-invalid-value"})
        assert malformed.status_code == 422
        assert "secret-invalid-value" not in malformed.text


def test_control_plane_rejects_cross_origin_and_non_json_requests(tmp_path):
    with TestClient(create_app(settings(tmp_path), tmp_path / "control")) as client:
        client.get("/")
        assert client.post("/api/stop", json={}, headers={"Origin": "https://untrusted.example"}).status_code == 403
        assert client.post("/api/stop", content="").status_code == 415
        assert client.post("/api/stop", json={}).json()["status"] == "IDLE"
        assert client.get("/", headers={"Host": "untrusted.example"}).status_code == 403
        assert "frame-ancestors 'none'" in client.get("/").headers["Content-Security-Policy"]


def test_reports_serve_real_evidence_and_reject_traversal(tmp_path):
    cfg = settings(tmp_path)
    folder = cfg.runs_dir / "run_123456abcdef"
    folder.mkdir(parents=True)
    report = {"assertions": [{"rule_id": "LOGIN_PROTECTED", "outcome": "PASS"}], "execution_mode": "llm"}
    (folder / "report.json").write_text(json.dumps(report))
    (folder / "screenshots").mkdir()
    (folder / "screenshots/check.png").write_bytes(b"test-evidence")
    secret = tmp_path / "private.json"
    secret.write_text('{"secret":"private"}')
    with TestClient(create_app(cfg, tmp_path / "control")) as client:
        client.get("/")
        data = client.get("/api/runs/run_123456abcdef/report").json()
        assert data["report"] == report
        assert data["evidence"] == ["screenshots/check.png"]
        assert client.get("/api/runs/run_123456abcdef/artifacts/screenshots/check.png").content == b"test-evidence"
        assert client.get("/api/runs/run_123456abcdef/artifacts/..%2F..%2Fprivate.json").status_code == 404
        assert client.get("/api/runs/invalid/report").status_code == 404


def test_version_mismatch_blocks_run_before_network_or_database(tmp_path):
    dashboard = Dashboard(settings(tmp_path), tmp_path / "control")
    source = build("clean", tmp_path / "source")
    project = Project(app_source=str(source), source_version="wrong-version")
    dashboard.projects["test"] = {"config": project.model_dump(), "secrets": {}}
    with pytest.raises(ValueError, match="version"):
        dashboard.preflight("test")
    assert not dashboard.running()


def test_stop_signal_prevents_the_next_tool_action(tmp_path):
    stop = tmp_path / "stop"
    stop.touch()
    tools = ToolLayer.__new__(ToolLayer)
    tools.cfg = SimpleNamespace(stop_file=stop)
    tools.board = Blackboard(run_id="fixture", mode="test")
    with pytest.raises(BudgetExpired, match="Stopped by user"):
        tools.consume()
    assert tools.board.budgets["actions"] == 0


def test_authentication_workflow_only_schedules_selected_checks(tmp_path):
    from qa_poc.agents import Team
    team = Team.__new__(Team)
    team.cfg = SimpleNamespace(workflow="authentication")
    team.board = Blackboard(run_id="fixture", mode="test", target_rules=["LOGIN_PROTECTED", "LOGOUT_REVOKES_SESSION"])
    team.tools = SimpleNamespace(probes={})
    jobs = team.enabled()
    assert jobs["ui_explorer"] == ["OBSERVE_LOGIN_PAGE"]
    assert jobs["api_explorer"] == ["DESCRIBE_ENDPOINT"]
    assert jobs["auth_db_explorer"] == team.board.target_rules
    assert jobs["diagnosis"] == ["READ_RUN_SUMMARY"]


def test_dashboard_recovers_worker_heartbeat_and_can_stop_it(tmp_path):
    cfg = settings(tmp_path)
    root = tmp_path / "dashboard"
    run = root / "run_123456abcdef"
    run.mkdir(parents=True)
    import time
    (run / "worker.json").write_text(json.dumps({"status": "RUNNING", "heartbeat": time.time()}))
    dashboard = Dashboard(cfg, root)
    assert dashboard.running()
    assert dashboard.current_id == run.name
    assert dashboard.stop()["status"] == "STOPPING"
    assert (run / "stop").exists()
