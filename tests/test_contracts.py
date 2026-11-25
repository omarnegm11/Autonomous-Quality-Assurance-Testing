import json
from pathlib import Path
from types import SimpleNamespace
import pytest
from pydantic import ValidationError
from qa_poc.builds import build, digest
from qa_poc.config import Settings
from qa_poc.schemas import Blackboard, Decision
from qa_poc.storage import redact
from qa_poc.tools import BudgetExpired, PERMISSIONS, ToolLayer


def settings(**changes):
    return Settings(_env_file=None,
        test_database_url="postgresql+psycopg://app:p@localhost/test_app",
        agent_state_database_url=changes.pop("agent_state_database_url", "postgresql+psycopg://state:p@localhost/test_state"),
        verifier_database_url="postgresql+psycopg://reader:p@localhost/test_app",
        test_user_a_password="a", test_user_b_password="b", **changes)


def test_database_separation_is_enforced():
    with pytest.raises(ValidationError, match="separate databases"):
        settings(agent_state_database_url="postgresql+psycopg://state:p@localhost/test_app")


def test_unknown_role_and_proposal_fields_rejected():
    with pytest.raises(ValidationError):
        Decision(agent="admin", goal_id="x", tool="sql", reason="x")
    with pytest.raises(ValidationError):
        Decision(agent="diagnosis", goal_id="x", tool="search_source", reason="x", command="rm")
    assert "call_allowed_endpoint" not in PERMISSIONS["diagnosis"]


def test_builds_are_distinct_source_matched_and_unlabelled(tmp_path):
    manifests = []
    for kind in ("clean", "visibility", "ownership"):
        source = build(kind, tmp_path / kind)
        manifest = json.loads((source / "manifest.json").read_text())
        assert manifest["source_digest"] == digest(source)
        assert set(manifest) == {"source_commit", "source_digest", "app_version"}
        manifests.append(manifest["app_version"])
    assert len(set(manifests)) == 3


def test_source_traversal_and_mismatch_are_rejected(tmp_path):
    source = build("clean", tmp_path / "build")
    tools = ToolLayer.__new__(ToolLayer)
    tools.cfg = SimpleNamespace(app_source=source)
    tools.board = Blackboard(run_id="test", mode="test", source_verified=True)
    with pytest.raises(ValueError, match="outside"):
        tools._source("../../.env")
    (source / "tasks.js").write_text("changed")
    with pytest.raises(ValueError, match="changed"):
        tools._source("tasks.js")
    assert not tools.board.source_verified


def test_secrets_are_redacted_recursively():
    data = redact({"nested": [{"password": "secret", "Cookie": "session"}],
                   "error": "failed postgresql+psycopg://user:pass@localhost/test",
                   "model_tokens": 42, "access_token": "raw", "provider_message": "invalid known-secret"}, ("known-secret",))
    assert "secret" not in json.dumps(data)
    assert "user:pass" not in json.dumps(data)
    assert "known-secret" not in json.dumps(data)
    assert data["model_tokens"] == 42
    assert data["access_token"] == "[REDACTED]"


def test_budget_stops_before_execution():
    tools = ToolLayer.__new__(ToolLayer)
    tools.deadline = 0
    tools.cfg = SimpleNamespace(max_actions=60)
    tools.board = Blackboard(run_id="test", mode="test")
    with pytest.raises(BudgetExpired):
        tools.consume()
    assert tools.board.budgets["actions"] == 0


def test_delete_hold_prevents_losing_verification_evidence():
    tools = ToolLayer.__new__(ToolLayer)
    tools.board = Blackboard(run_id="test", mode="test")
    with pytest.raises(ValueError, match="Deletion held"):
        tools._may_delete()
