"""llm.complete: structured output that is cut off (max_tokens) or invalid is retried once, then degrades.

Regression for "write an article on best pasta places in pakistan": the planner's brief hit its token
cap, the JSON was cut off, and the parse error crashed the whole turn.
"""
from types import SimpleNamespace

import pytest

import llm
import run_context
from agents import content_planner as planner_mod
from middleware import cost_guard

SCHEMA = {"type": "object", "properties": {"title": {"type": "string"}}, "required": ["title"],
          "additionalProperties": False}


def _response(text: str, stop_reason: str = "end_turn"):
    return SimpleNamespace(
        stop_reason=stop_reason,
        usage=SimpleNamespace(input_tokens=10, output_tokens=5),
        content=[SimpleNamespace(type="text", text=text)],
    )


@pytest.fixture
def run():
    cost_guard.reset()
    token = run_context.set_run(run_context.RunContext(run_id="t", user_id="u", thread_id="th"))
    yield
    run_context.reset_run(token)


@pytest.fixture
def api(monkeypatch):
    """Fake Messages API: returns queued responses and records each request's max_tokens."""
    calls: list[int] = []
    queue: list = []

    def create(**params):
        calls.append(params["max_tokens"])
        return queue.pop(0)

    monkeypatch.setattr(llm.client.messages, "create", create)
    return SimpleNamespace(calls=calls, queue=queue)


def test_cut_off_json_is_retried_with_a_bigger_cap(run, api):
    api.queue += [_response('{"title": "Best pas', "max_tokens"), _response('{"title": "Best pasta"}')]
    assert llm.complete("sys", [{"role": "user", "content": "x"}], max_tokens=900, schema=SCHEMA) == {"title": "Best pasta"}
    assert api.calls == [900, 1800]


def test_invalid_json_is_retried_once(run, api):
    api.queue += [_response("not json at all"), _response('{"title": "ok"}')]
    assert llm.complete("sys", [{"role": "user", "content": "x"}], max_tokens=300, schema=SCHEMA) == {"title": "ok"}
    assert api.calls == [300, 600]


def test_second_failure_raises_bad_output_not_a_json_error(run, api):
    api.queue += [_response('{"title": "a', "max_tokens"), _response('{"title": "b', "max_tokens")]
    with pytest.raises(llm.LLMBadOutput):
        llm.complete("sys", [{"role": "user", "content": "x"}], max_tokens=900, schema=SCHEMA)
    assert api.calls == [900, 1800]


def test_bad_output_is_caught_by_existing_refusal_handlers():
    assert issubclass(llm.LLMBadOutput, llm.LLMRefusal)


def test_retry_cap_is_bounded(run, api):
    api.queue += [_response("{", "max_tokens"), _response('{"title": "ok"}')]
    llm.complete("sys", [{"role": "user", "content": "x"}], max_tokens=3000, schema=SCHEMA)
    assert api.calls == [3000, llm.MAX_RETRY_TOKENS]


def test_plain_text_calls_are_not_retried(run, api):
    api.queue += [_response("a long article that was cut", "max_tokens")]
    assert llm.complete("sys", [{"role": "user", "content": "x"}], max_tokens=50) == "a long article that was cut"
    assert api.calls == [50]


def test_planner_degrades_instead_of_crashing(run, monkeypatch):
    def bad(*args, **kwargs):
        raise llm.LLMBadOutput("cut off twice")

    monkeypatch.setattr(llm, "complete", bad)
    monkeypatch.setattr(planner_mod.web, "web_search", lambda *a, **k: [])
    out = planner_mod.content_planner({"user_id": "u", "keyword": "best pasta places in pakistan"})
    assert "try again" in out["reply"]
    assert out.get("stage") != "planned"
