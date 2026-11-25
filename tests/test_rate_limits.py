from types import SimpleNamespace
import httpx
import pytest
from qa_poc.rate_limits import invoke_with_backoff, retry_delay


class Limited(Exception):
    status_code = 429

    def __init__(self, message="rate limited", headers=None):
        super().__init__(message)
        self.response = httpx.Response(429, headers=headers or {})


def test_honors_header_and_groq_message():
    assert retry_delay(Limited(headers={"retry-after": "2"}), 0) == 2.25
    assert retry_delay(Limited("Please try again in 5.014s."), 0) == 5.264


def test_recovers_without_reexecuting_tools(monkeypatch):
    clock = [0.0]
    monkeypatch.setattr("qa_poc.rate_limits.time.monotonic", lambda: clock[0])
    monkeypatch.setattr("qa_poc.rate_limits.time.sleep", lambda seconds: clock.__setitem__(0, clock[0] + seconds))
    calls, events = [], []

    def invoke(*args, **kwargs):
        calls.append(1)
        if len(calls) == 1:
            raise Limited(headers={"retry-after": "2"})
        return "success"

    cfg = SimpleNamespace(max_seconds=30, llm_rate_limit_retries=2, llm_max_retry_wait_seconds=10)
    tools = SimpleNamespace(deadline=30, store=SimpleNamespace(append=lambda *args: events.append(args)))
    assert invoke_with_backoff(SimpleNamespace(invoke=invoke), [], cfg, tools, "ui_explorer") == "success"
    assert len(calls) == 2 and clock[0] == 2.25
    assert events[0][1]["event"] == "rate_limit_wait"


@pytest.mark.parametrize("deadline,wait", [(2, "3"), (100, "86400")])
def test_does_not_wait_beyond_budget_or_daily_quota(monkeypatch, deadline, wait):
    monkeypatch.setattr("qa_poc.rate_limits.time.monotonic", lambda: 0)
    monkeypatch.setattr("qa_poc.rate_limits.time.sleep", lambda _: pytest.fail("Must not wait"))
    error = Limited(headers={"retry-after": wait})

    def invoke(*args, **kwargs):
        raise error

    cfg = SimpleNamespace(max_seconds=100, llm_rate_limit_retries=3, llm_max_retry_wait_seconds=60)
    with pytest.raises(Limited):
        invoke_with_backoff(SimpleNamespace(invoke=invoke), [], cfg, SimpleNamespace(deadline=deadline), "ui_explorer")
