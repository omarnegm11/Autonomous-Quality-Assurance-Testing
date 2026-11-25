"""Bounded provider-rate-limit recovery; never replays application mutations."""
import re
import time


def retry_delay(exc, attempt):
    response = getattr(exc, "response", None)
    header = response.headers.get("retry-after") if response is not None else None
    if header:
        try:
            return max(0.0, float(header)) + 0.25
        except ValueError:
            pass
    match = re.search(r"try again in\s+(\d+(?:\.\d+)?)\s*s", str(exc), re.I)
    return float(match.group(1)) + 0.25 if match else min(5 * (2 ** attempt), 30)


def invoke_with_backoff(model, messages, cfg, tools, role):
    retries = getattr(cfg, "llm_rate_limit_retries", 3)
    max_wait = getattr(cfg, "llm_max_retry_wait_seconds", 60)
    for attempt in range(retries + 1):
        if hasattr(tools, "check_stop"):
            tools.check_stop()
        try:
            return model.invoke(messages, config={"callbacks": []})
        except Exception as exc:
            if getattr(exc, "status_code", None) != 429 or attempt >= retries:
                raise
            delay = retry_delay(exc, attempt)
            deadline = getattr(tools, "deadline", time.monotonic() + cfg.max_seconds)
            if delay > max_wait or time.monotonic() + delay + 2 >= deadline:
                raise
            tools.store.append("provider_events.jsonl", {"agent": role, "event": "rate_limit_wait",
                "status": 429, "retry": attempt + 1, "wait_seconds": delay})
            print(f"{role}: provider rate limit; retry {attempt + 1}/{retries} in {delay:.1f}s", flush=True)
            if hasattr(tools, "show_status"):
                tools.show_status(f"{role}: waiting {delay:.1f}s for provider quota")
            # Small slices preserve interruption handling; outer finally performs cleanup.
            wake = time.monotonic() + delay
            while time.monotonic() < wake:
                if hasattr(tools, "check_stop"):
                    tools.check_stop()
                time.sleep(max(0.0, min(1.0, wake - time.monotonic())))
