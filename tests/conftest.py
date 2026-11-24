"""Test setup: no network, no database, no real Anthropic calls.

Env vars are set before any app module is imported, so pydantic-settings picks them up
over api/.env. An empty DATABASE_URL makes the graph use the in-memory checkpointer.
"""
import os

os.environ["DATABASE_URL"] = ""
os.environ["ANTHROPIC_API_KEY"] = "test-key"
os.environ["TAVILY_API_KEY"] = "test-key"

import pytest  # noqa: E402

import graph  # noqa: E402
import llm  # noqa: E402


class FakeLLM:
    """Stands in for llm.complete. Records calls; returns the queued or default reply."""

    def __init__(self):
        self.calls: list[dict] = []
        self.queue: list = []
        self.default = {"route": "chat", "reply": "Hello from the fake model."}
        self.handlers: list[tuple[str, object]] = []

    def on(self, system_contains: str, result) -> None:
        """Answer calls whose system prompt contains the text. `result` may be a callable(system, messages)."""
        self.handlers.append((system_contains, result))

    def __call__(self, system, messages, max_tokens=400, schema=None):
        self.calls.append({"system": system, "messages": messages, "schema": schema})
        for needle, handler in self.handlers:
            if needle in system:
                result = handler(system, messages) if callable(handler) else handler
                break
        else:
            result = self.queue.pop(0) if self.queue else self.default
        if schema is None and isinstance(result, dict):
            return result.get("reply", "")
        return result


@pytest.fixture
def fake_llm(monkeypatch) -> FakeLLM:
    fake = FakeLLM()
    monkeypatch.setattr(llm, "complete", fake)
    return fake


@pytest.fixture
def app_graph():
    """Fresh in-memory graph per test."""
    graph.init()
    yield graph
    graph.shutdown()


@pytest.fixture(autouse=True)
def clean_stores():
    """The in-process store fallback is global; reset it around every test."""
    from middleware import cost_guard, rate_limit
    from stores import approvals, briefs, drafts, keywords, metrics, posts, spans, users
    from stores import memory as memory_store
    stores = (keywords, briefs, drafts, memory_store, approvals, posts, metrics, spans, users)
    for store in stores:
        store.clear_memory()
    rate_limit.reset()
    cost_guard.reset()
    yield
    for store in stores:
        store.clear_memory()
    rate_limit.reset()
    cost_guard.reset()


@pytest.fixture(autouse=True)
def clean_web_cache():
    """Tavily cache and credit counters are process-global; reset them around every test."""
    from tools import web
    web.reset()
    yield
    web.reset()
