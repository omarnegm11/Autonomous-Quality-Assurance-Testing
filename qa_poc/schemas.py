from datetime import datetime, timezone
from enum import Enum
from typing import Any, Literal
from pydantic import BaseModel, ConfigDict, Field


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Outcome(str, Enum):
    PASS = "PASS"
    FAIL = "FAIL"
    BLOCKED = "BLOCKED"
    INCONCLUSIVE = "INCONCLUSIVE"


Role = Literal["ui_explorer", "api_explorer", "auth_db_explorer", "diagnosis"]


class Decision(BaseModel):
    model_config = ConfigDict(extra="forbid")
    agent: Role
    goal_id: str
    tool: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    reason: str = Field(min_length=1, max_length=1200)


class ToolResult(BaseModel):
    ok: bool
    action_id: str
    observation: dict[str, Any]
    evidence_refs: list[str] = Field(default_factory=list)


class Check(BaseModel):
    rule_id: str
    workflow_id: str
    outcome: Outcome
    expected: Any
    actual: Any
    evidence_refs: list[str] = Field(default_factory=list)
    action_id: str = ""


class Message(BaseModel):
    sender: Role
    performative: Literal["inform", "request", "complete"] = "inform"
    run_id: str
    event: str
    record: dict[str, Any] = Field(default_factory=dict)
    pending_checks: list[str] = Field(default_factory=list)


RULES = {
    "LOGIN_PROTECTED": ("WF-01", "Valid login grants GET /api/tasks: 200"),
    "LOGOUT_REVOKES_SESSION": ("WF-01", "Old session after logout receives 401"),
    "TASK_PERSISTENCE": ("WF-02", "Saved incomplete task remains visible after reload"),
    "API_VALUES_MATCH": ("WF-02", "GET task: 200, with exact saved ID/title/owner/completed"),
    "DB_VALUES_MATCH": ("WF-02", "Database row matches saved ID/title/owner/completed"),
    "TASK_EDIT": ("WF-02", "Owner PATCH returns 200 and new title persists in API/DB/UI"),
    "TASK_DELETE": ("WF-02", "Owner DELETE returns 204; API 404, row absent, UI absent"),
    "OWNER_ONLY_EDIT": ("WF-03", "B PATCH of A's record: 403, saved values unchanged"),
    "OWNER_UI_ISOLATION": ("WF-03", "B cannot see or edit A's task in UI"),
}


class Blackboard(BaseModel):
    run_id: str
    mode: str
    target_rules: list[str] = Field(default_factory=lambda: list(RULES))
    app_version: str = ""
    source_commit: str = ""
    source_verified: bool = False
    started_at: str = Field(default_factory=now)
    rules: dict[str, Any] = Field(default_factory=lambda: RULES.copy())
    goals: list[str] = Field(default_factory=lambda: list(RULES))
    coverage: list[str] = Field(default_factory=list)
    observations: list[dict[str, Any]] = Field(default_factory=list)
    identities: list[str] = Field(default_factory=lambda: ["user_a", "user_b"])
    session_refs: dict[str, str] = Field(default_factory=dict)
    records: dict[int, dict[str, Any]] = Field(default_factory=dict)
    action_history: list[dict[str, Any]] = Field(default_factory=list)
    pending_checks: list[str] = Field(default_factory=list)
    reservations: dict[str, str] = Field(default_factory=dict)
    failures: list[str] = Field(default_factory=list)
    diagnoses: dict[str, dict[str, Any]] = Field(default_factory=dict)
    agent_reviews: dict[str, dict[str, Any]] = Field(default_factory=dict)
    agent_status: dict[str, dict[str, Any]] = Field(default_factory=dict)
    assertions: dict[str, Check] = Field(default_factory=dict)
    messages: list[Message] = Field(default_factory=list)
    cleanup_records: list[int] = Field(default_factory=list)
    budgets: dict[str, int] = Field(default_factory=lambda: {"actions": 0, "model_tokens": 0})
    stop_reason: str = ""
