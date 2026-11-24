"""Tavily credit protection: cache, per-run cap, daily cap."""
import httpx
import pytest

import run_context
from agents import keyword_scout
from config import settings
from tools import ToolUnavailable, web
from tools import keywords as keyword_tool


class Resp:
    def __init__(self, payload, status=200):
        self._p, self.status_code = payload, status

    def json(self):
        return self._p

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError("boom", request=None, response=None)


@pytest.fixture
def net(monkeypatch):
    monkeypatch.setattr(settings, "tavily_api_key", "k")
    calls = []

    def post(url, json=None, headers=None, timeout=None):
        calls.append(url)
        return Resp({"results": [{"title": "T", "url": "http://a.com", "content": "c", "raw_content": "page"}]})
    monkeypatch.setattr(httpx, "post", post)
    return calls


@pytest.fixture
def run():
    ctx = run_context.RunContext(run_id="r", user_id="alice", thread_id="t")
    token = run_context.set_run(ctx)
    yield ctx
    run_context.reset_run(token)


def test_repeated_search_is_served_from_cache(net, run):
    first = web.web_search("Hiking Boots")
    second = web.web_search("  hiking boots ")          # same query after normalising
    assert first == second and len(net) == 1 and run.searches == 1
    assert run.tool_calls[1]["args"]["cached"] is True


def test_different_result_counts_are_cached_separately(net):
    web.web_search("boots", max_results=3)
    web.web_search("boots", max_results=5)
    assert len(net) == 2


def test_failed_requests_are_not_cached(monkeypatch):
    monkeypatch.setattr(settings, "tavily_api_key", "k")
    state = {"n": 0}

    def post(*a, **k):
        state["n"] += 1
        return Resp({}, status=500) if state["n"] == 1 else Resp({"results": []})
    monkeypatch.setattr(httpx, "post", post)
    with pytest.raises(ToolUnavailable):
        web.web_search("boots")
    assert web.web_search("boots") == [] and state["n"] == 2


def test_per_run_cap_stops_further_searches_but_not_cache_hits(net, run, monkeypatch):
    monkeypatch.setattr(settings, "tavily_max_searches_per_run", 2)
    web.web_search("a")
    web.web_search("b")
    with pytest.raises(ToolUnavailable, match="this request"):
        web.web_search("c")
    assert web.web_search("a")                           # cache hit costs nothing
    assert len(net) == 2


def test_daily_cap(net, monkeypatch):
    monkeypatch.setattr(settings, "tavily_daily_limit", 1)
    web.web_search("a")
    with pytest.raises(ToolUnavailable, match="daily"):
        web.web_search("b")
    assert web.usage() == {"day": web.usage()["day"], "used": 1, "limit": 1}


def test_fetch_is_cached(net):
    web.web_fetch("http://a.com")
    web.web_fetch("http://a.com")
    assert len(net) == 1


def test_writing_an_article_spends_one_search_in_the_scout(net, monkeypatch):
    monkeypatch.setattr(keyword_tool, "keyword_search", lambda t, l=None, limit=20: [
        {"keyword": f"idea {i}", "demand": 1 - i / 10, "intent": 0.2, "source": "autocomplete"} for i in range(6)])
    keyword_scout.keyword_scout({"user_id": "alice", "route": "write_article", "topic": "hiking boots"})
    assert len(net) == 1
    net.clear()
    web.reset()
    keyword_scout.keyword_scout({"user_id": "alice", "route": "keyword_research", "topic": "hiking boots"})
    assert len(net) == keyword_scout.COMPETITION_CHECKS
