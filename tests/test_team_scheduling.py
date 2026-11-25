import time
from types import SimpleNamespace
from qa_poc.agents import Team, GOALS
from qa_poc.schemas import Blackboard, Check, Outcome, RULES
from qa_poc.tools import ToolLayer


def team_fixture():
    team = Team.__new__(Team)
    team.board = Blackboard(run_id="scheduler-fixture", mode="test_double")
    team.cfg = SimpleNamespace(max_actions=60)
    team.tools = SimpleNamespace(deadline=time.monotonic() + 100, probes={}, last_responses={}, cfg=team.cfg, board=team.board,
        store=SimpleNamespace(save=lambda: None))
    team.cursor = 0
    team.idle = {role: 0 for role in GOALS}
    team.activations = {role: 0 for role in GOALS}
    return team


def test_clean_run_diagnosis_reviews_evidence_without_inventing_failure():
    team = team_fixture()
    assert team.enabled()["diagnosis"] == ["READ_RUN_SUMMARY"]
    team.board.agent_reviews["diagnosis"] = {"overview_read": True}
    assert not team.enabled()["diagnosis"]
    team.board.assertions = {rule: Check(rule_id=rule, workflow_id=wf, outcome=Outcome.PASS,
        expected="fixture", actual="fixture") for rule, (wf, _) in RULES.items()}
    assert team.enabled()["diagnosis"] == ["SUBMIT_RUN_REVIEW"]
    assert not team.board.failures


def test_ready_specialists_alternate_while_clean_review_waits_its_turn():
    team = team_fixture()
    team.enabled = lambda: {role: ["READY"] for role in GOALS}
    assert [team.schedule({})["role"] for _ in range(4)] == list(GOALS)


def test_successful_ui_steps_do_not_get_blocked_before_record_creation():
    team = team_fixture()

    def invoke(*args, **kwargs):
        team.board.action_history.append({"result": {"ok": True}})

    team.subgraph = SimpleNamespace(invoke=invoke)
    for _ in range(4):
        team.activate({"role": "ui_explorer"})
    assert team.idle["ui_explorer"] == 0
    assert not team.board.records


def test_closed_inactive_window_does_not_block_active_viewer():
    tools = ToolLayer.__new__(ToolLayer)
    tools.browser = SimpleNamespace(is_connected=lambda: True)
    tools.active_identity = "user_a"
    tools.pages = {"user_a": SimpleNamespace(is_closed=lambda: False), "user_b": SimpleNamespace(is_closed=lambda: True)}
    # Recovery members intentionally absent: a healthy active page needs no restart.
    tools.ensure_browser()
