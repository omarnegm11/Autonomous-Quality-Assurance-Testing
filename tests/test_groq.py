"""Exercise the real Groq adapter against a mocked HTTP service, with no hosted keys."""
import json
import os
from types import SimpleNamespace
import httpx
import pytest
from pydantic import SecretStr
from qa_poc import agents
from qa_poc.schemas import Blackboard
from qa_poc.tools import ToolLayer


@pytest.mark.parametrize("base_url", ["https://api.groq.com", "https://api.groq.com/openai/v1", ""])
def test_native_groq_sends_single_tool_request_without_langchain_key(monkeypatch, base_url):
    for name in ("LANGCHAIN_API_KEY", "LANGSMITH_API_KEY", "GROQ_API_KEY", "GROQ_BASE_URL", "OPENAI_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("LANGSMITH_TRACING", "true")
    monkeypatch.setenv("LANGCHAIN_TRACING_V2", "true")
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(200, json={
            "id": "mock-groq-completion", "object": "chat.completion", "created": 0,
            "model": "qwen/qwen3.8-27b", "choices": [{"index": 0, "finish_reason": "tool_calls", "message": {
                "role": "assistant", "content": None, "tool_calls": [{"id": "groq-tool-1", "type": "function",
                    "function": {"name": "observe_page", "arguments": json.dumps({"action_reason": "Inspect current page"})}}]}}],
            "usage": {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120}})

    original_factory = agents.init_chat_model
    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        def factory(*args, **kwargs):
            return original_factory(*args, **kwargs, http_client=client)

        monkeypatch.setattr(agents, "init_chat_model", factory)
        board = Blackboard(run_id="groq-fixture", mode="test_double")
        registry = ToolLayer.__new__(ToolLayer).registry
        store = SimpleNamespace(write=lambda *args: None, append=lambda *args: None)
        tools = SimpleNamespace(board=board, store=store, registry=registry, probes={},
            observe_page=lambda: {"login_visible": True}, active_identity="user_a", remaining=lambda: 30)
        cfg = SimpleNamespace(llm_api_key=SecretStr("mock-groq-key"), llm_model="qwen/qwen3.8-27b",
            llm_provider="groq", llm_base_url=base_url, llm_reasoning_effort="none",
            max_seconds=300, max_model_tokens=30000)
        team = agents.Team(cfg, tools)
        result = team.choose({"role": "ui_explorer", "steps": 0})

    assert len(requests) == 1
    assert requests[0].url.host == "api.groq.com"
    assert requests[0].url.path == "/openai/v1/chat/completions"
    assert requests[0].headers["authorization"] == "Bearer mock-groq-key"
    body = json.loads(requests[0].content)
    assert body["model"] == "qwen/qwen3.8-27b"
    assert body["reasoning_effort"] == "none"
    assert body["reasoning_format"] == "hidden"
    assert body["parallel_tool_calls"] is False
    names = {tool["function"]["name"] for tool in body["tools"]}
    assert "observe_page" in names and "search_source" not in names
    assert result["call"]["id"] == "groq-tool-1"
    assert result["call"]["name"] == "observe_page"
    assert board.budgets["model_tokens"] == 120
    assert os.environ["LANGSMITH_TRACING"] == "false"
    assert os.environ["LANGCHAIN_TRACING_V2"] == "false"


def test_missing_groq_key_stops_before_model_initialization(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("No provider initialization should occur without credentials")

    monkeypatch.setattr(agents, "init_chat_model", forbidden)
    cfg = SimpleNamespace(llm_api_key=SecretStr(""), llm_model="qwen/qwen3.8-27b")
    with pytest.raises(ValueError, match="LLM_API_KEY"):
        agents.Team(cfg, None)
