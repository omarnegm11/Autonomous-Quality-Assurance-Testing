"""Contract test for observation-dependent looping using a labelled test double, not a live LLM."""
from types import SimpleNamespace
from langchain_core.messages import AIMessage, ToolMessage
from pydantic import SecretStr
from qa_poc.agents import Team
from qa_poc.schemas import Blackboard, ToolResult


class MemoryStore:
    def __init__(self):
        self.events = []

    def write(self, name, data):
        self.events.append((name, data))

    def append(self, name, data):
        self.events.append((name, data))


class ObservationModel:
    def bind_tools(self, registry):
        return self

    def invoke(self, messages, config):
        previous = [m for m in messages if isinstance(m, ToolMessage)]
        if not previous:
            name, args = "click_element", {"testid": "create"}
        elif '"validation"' in previous[-1].content:
            name, args = "fill_field", {"testid": "new-title", "value": "Responding to validation"}
        else:
            return AIMessage(content="Activation complete")
        args["action_reason"] = "React to current observation in test fixture"
        return AIMessage(content="", tool_calls=[{"name": name, "args": args, "id": f"fixture-{len(previous)}", "type": "tool_call"}])


def test_subgraph_adapts_to_result_and_preserves_call_linkage(monkeypatch):
    monkeypatch.setattr("qa_poc.agents.init_chat_model", lambda *a, **kw: ObservationModel())
    board = Blackboard(run_id="fixture", mode="test_double")
    store = MemoryStore()
    executed = []

    def execute(proposal, tool_call_id):
        executed.append((proposal, tool_call_id))
        observation = {"validation": "Task title required"} if proposal.tool == "click_element" else {"filled": True}
        return ToolResult(ok=True, action_id=str(len(executed)), observation=observation)

    tools = SimpleNamespace(board=board, store=store, registry=lambda *a, **kw: [],
        observe_page=lambda: {"login_visible": False, "fields": [{"testid": "new-title", "value": ""}]},
        remaining=lambda: 30, active_identity="user_a", probes={}, execute=execute)
    cfg = SimpleNamespace(llm_api_key=SecretStr("test-double-key"), llm_model="test-double",
        llm_provider="test-double", max_seconds=300, llm_base_url="", max_model_tokens=30000,
        specialist_actions_per_activation=6)
    team = Team(cfg, tools)
    team.subgraph.invoke({"role": "ui_explorer", "steps": 0, "finished": False})
    assert [p.tool for p, _ in executed] == ["click_element", "fill_field"]
    assert executed[1][0].arguments["value"] == "Responding to validation"
    assert [call_id for _, call_id in executed] == ["fixture-0", "fixture-1"]
    results = [m for m in team.messages["ui_explorer"] if isinstance(m, ToolMessage)]
    assert [m.tool_call_id for m in results] == ["fixture-0", "fixture-1"]
