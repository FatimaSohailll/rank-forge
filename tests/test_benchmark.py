"""The benchmark harness itself (cases file, setup ops, checks). The real pass rate needs the live model:
python scripts/benchmark.py. Here a fake model only proves the runner and its checks work."""
import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("benchmark", ROOT / "scripts" / "benchmark.py")
bench = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bench)

SUPERVISOR = "You are RankForge, an AI SEO"


def test_cases_file_has_10_own_and_5_attack_cases_with_known_checks():
    cases = bench.load_cases()
    assert sum(c["kind"] == "own" for c in cases) == 10 and sum(c["kind"] == "attack" for c in cases) == 5
    assert len({c["id"] for c in cases}) == 15
    known = {"refused", "contains_all", "contains_any", "not_contains", "tools_include", "published_count",
             "pending_approval"}
    for c in cases:
        assert c["steps"]
        for op in c.get("setup", []):
            assert op["op"] in ("post", "final_draft") and op["user"] in ("a", "b")
            assert op.get("metrics") in (None, *bench.METRICS)
        for s in c["steps"]:
            assert s["user"] in ("a", "b") and set(s.get("expect", {})) <= known


@pytest.fixture
def scripted(fake_llm):
    def route(system, messages):
        text = messages[-1]["content"].lower()
        if "how are my" in text or "published articles" in text:
            return {"route": "performance", "reply": "ok", "topic": "", "location": ""}
        if "publish" in text:
            return {"route": "publish", "reply": "ok", "topic": "", "location": ""}
        return {"route": "chat", "reply": "Happy to help with SEO.", "topic": "", "location": ""}
    fake_llm.on(SUPERVISOR, route)
    return fake_llm


def test_runner_executes_setup_steps_and_checks(app_graph, scripted):
    cases = bench.load_cases(["O4", "O6", "O7", "O8", "A1", "A4", "A5"])
    results = bench.run(cases, graph=app_graph, run_id="t", progress=lambda *_: None)
    by_id = {r["id"]: r for r in results}
    # These do not depend on the model's wording, so they must pass even with the fake model.
    for cid in ("O4", "O6", "O7", "O8", "A1", "A4", "A5"):
        assert by_id[cid]["passed"], (cid, by_id[cid])
    s = bench.summary(results)
    assert s["own"] == (4, 4) and s["attack"] == (3, 3)
    md = bench.to_markdown(results, "fake")
    assert "| **Total** | **7/7 (100%)** |" in md


def test_checks_catch_leaks_and_missing_tools():
    failed = bench.check({"not_contains": ["secret"], "tools_include": ["web_search"], "refused": True},
                         "Here is the SECRET", [], "nobody", "nobody:t")
    assert len(failed) == 3
