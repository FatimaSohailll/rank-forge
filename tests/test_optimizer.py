"""Monitor lane + Optimizer: trackers, decisions, the re-write and re-target loops, the loop cap."""
from datetime import date, timedelta

import pytest

from agents import click_tracker, optimizer, rank_tracker
from stores import approvals, briefs, drafts, metrics, posts

SUPERVISOR = "You are RankForge, an AI SEO"
PLANNER = "You are the Content Planner"
WRITER = "You are the Writer"

TODAY = date.today()

# Same shapes as db/seed.sql: (slug, position(d), sessions(d)) for d = 0..29, d=29 is today.
PATTERNS = {
    "boots": (lambda d: 40 - d, lambda d: 20 + d * 4),           # improving -> hold
    "tent": (lambda d: 18 + d % 3, lambda d: 60 + (d % 5) * 3),   # stuck on page 2 -> rewrite
    "checklist": (lambda d: 25 + d, lambda d: 90 - d * 2),       # sinking, low traffic -> retarget
    "espresso": (lambda d: 12 - d // 5, lambda d: 120 + d * 3),  # top 10 -> hold
}


def seed(user="alice", only=None, rewrite_counts=None):
    ids = {}
    for name, (pos, sess) in PATTERNS.items():
        if only and name not in only:
            continue
        p = posts.create_post(user, f"{user}-{name}", f"{name.title()} guide", f"best {name}", "", "<p>x</p>", {})
        ids[name] = p["id"]
        for d in range(30):
            day = TODAY - timedelta(days=29 - d)
            metrics.add_ranking(user, p["id"], f"best {name}", pos(d), "simulated", day)
            metrics.add_clicks(user, p["id"], day, sess(d), int(sess(d) * 0.08))
        for _ in range((rewrite_counts or {}).get(name, 0)):
            posts.update_post(user, p["id"], f"{name.title()} guide", "", "<p>x</p>", {})
    return ids


def summaries(user="alice"):
    return (rank_tracker.summarise(metrics.rankings_for_user(user)),
            click_tracker.summarise(metrics.clicks_for_user(user)))


# ---------- pure decisions ----------

def test_decisions_match_the_seeded_trends():
    ids = seed()
    rows = {r["post_id"]: r for r in optimizer.assess("alice", *summaries())}
    assert rows[ids["boots"]]["decision"] == "hold" and "improving" in rows[ids["boots"]]["reason"]
    assert rows[ids["tent"]]["decision"] == "rewrite" and "stuck" in rows[ids["tent"]]["reason"]
    assert rows[ids["checklist"]]["decision"] == "retarget"
    assert rows[ids["espresso"]]["decision"] == "hold" and "top 10" in rows[ids["espresso"]]["reason"]
    ordered = [r["decision"] for r in optimizer.assess("alice", *summaries())]
    assert ordered == sorted(ordered, key=optimizer.PRIORITY.get)          # most urgent first


def test_too_little_data_holds():
    assert optimizer.decide(None, None)["decision"] == "hold"
    rank = {"recent_position": 60, "prior_position": None, "days_tracked": 3}
    assert "too early" in optimizer.decide(rank, {"sessions_7d": 1})["reason"]


def test_slipping_and_traffic_drop_trigger_rewrites():
    slip = {"recent_position": 15, "prior_position": 8, "days_tracked": 30}
    assert optimizer.decide(slip, {"sessions_7d": 500, "sessions_prev_7d": 500})["decision"] == "rewrite"
    steady = {"recent_position": 5, "prior_position": 5, "days_tracked": 30}
    drop = optimizer.decide(steady, {"sessions_7d": 100, "sessions_prev_7d": 400})
    assert drop["decision"] == "rewrite" and "fell 75%" in drop["reason"]


def test_loop_cap_escalates_to_a_human_instead_of_rewriting():
    stuck = {"recent_position": 20, "prior_position": 20, "days_tracked": 30}
    d = optimizer.decide(stuck, {"sessions_7d": 400, "sessions_prev_7d": 400}, rewrite_count=3, max_loops=3)
    assert d["decision"] == "hold" and d["escalate"] and "human" in d["reason"]


def test_trackers_summarise_windows():
    ids = seed(only=["boots"])
    rank, clicks = summaries()
    r, c = rank[0], clicks[0]
    assert r["post_id"] == ids["boots"] and r["days_tracked"] == 30
    assert r["recent_position"] < r["prior_position"]                     # improving
    assert c["sessions_7d"] == sum(20 + d * 4 for d in range(23, 30))
    assert c["sessions_prev_7d"] == sum(20 + d * 4 for d in range(16, 23))
    assert 0 < c["ctr_7d"] < 0.1


def test_users_only_see_their_own_signals():
    seed("alice", only=["tent"])
    seed("bob", only=["boots"])
    assert [r["title"] for r in optimizer.assess("bob", *summaries("bob"))] == ["Boots guide"]


# ---------- through the graph ----------

@pytest.fixture
def routed(fake_llm):
    def route(system, messages):
        text = messages[-1]["content"].lower()
        if "how are my" in text:
            return {"route": "performance", "reply": "x", "topic": "", "location": ""}
        if "optimi" in text or "rewrite" in text:
            topic = "tent" if "tent" in text else ""
            return {"route": "optimize", "reply": "x", "topic": topic, "location": ""}
        if "publish" in text:
            return {"route": "publish", "reply": "x", "topic": "", "location": ""}
        return {"route": "chat", "reply": "chat", "topic": "", "location": ""}
    fake_llm.on(SUPERVISOR, route)
    return fake_llm


def test_performance_report_lists_every_post_without_acting(app_graph, routed):
    seed()
    out = app_graph.run_turn("alice", "t1", "how are my posts doing?")
    assert "Tent guide" in out["reply"] and "rewrite" in out["reply"] and "retarget" in out["reply"]
    assert metrics.actions_for_user("alice") == []                       # report only: nothing logged
    assert not any(c for c in routed.calls if PLANNER in c["system"])


def test_performance_with_no_posts(app_graph, routed):
    out = app_graph.run_turn("alice", "t1", "how are my posts doing?")
    assert "don't have any published articles" in out["reply"]


def test_retarget_loop_goes_back_to_the_keyword_scout(app_graph, routed, monkeypatch):
    from tools import keywords as kw_tool
    monkeypatch.setattr(kw_tool, "keyword_search", lambda topic, location=None: [
        {"keyword": "day hike checklist for beginners", "demand": 0.9, "intent": 0.2, "source": "autocomplete"}])
    from agents import keyword_scout
    monkeypatch.setattr(keyword_scout, "competition_for", lambda k: 0.3)
    ids = seed(only=["checklist"])
    out = app_graph.run_turn("alice", "t1", "optimize my posts")
    assert out["reply"].startswith("Re-target:") and "day hike checklist for beginners" in out["reply"]
    log = metrics.actions_for_user("alice")
    assert log[0]["decision"] == "retarget" and log[0]["post_id"] == ids["checklist"]


def test_rewrite_loop_replans_writes_and_replaces_the_live_post_after_approval(app_graph, routed):
    ids = seed(only=["tent", "boots"])
    routed.on(PLANNER, {"title": "Best Tent Guide", "meta_description": "m", "angle": "more complete",
                        "word_count_target": 900, "outline": [{"heading": "Picks", "points": ["a"]}],
                        "faq": [], "affiliate_programs": []})
    routed.on(WRITER, "# Best Tent Guide\n\nBest tent advice for everyone. " + "Plain words here. " * 60)
    routed.on("You extract checkable", {"claims": []})
    routed.on("You are the Editor", "unused")

    out = app_graph.run_turn("alice", "t1", "rewrite the tent article")
    planner_call = next(c for c in routed.calls if PLANNER in c["system"])
    assert "REWRITE of a live article" in planner_call["messages"][0]["content"]
    assert "replace the live version" in out["reply"]
    final = drafts.latest_draft("alice", "final")
    assert briefs.get_brief("alice", final["brief_id"])["brief"]["rewrite_of"] == ids["tent"]
    assert metrics.actions_for_user("alice")[0]["decision"] == "rewrite"

    prompt = app_graph.run_turn("alice", "t1", "publish it")["reply"]
    assert "replace your live article" in prompt and "Tent guide" in prompt
    assert posts.get_for_user("alice", ids["tent"])["title"] == "Tent guide"     # unchanged until approved

    done = app_graph.run_turn("alice", "t1", "approve")["reply"]
    assert done.startswith("Updated:")
    tent = posts.get_for_user("alice", ids["tent"])
    assert tent["title"] == "Best Tent Guide" and tent["rewrite_count"] == 1 and tent["slug"] == "alice-tent"
    assert len(posts.list_for_user("alice")) == 2                                 # replaced, not added


def test_optimize_holds_when_the_loop_cap_is_reached(app_graph, routed):
    seed(only=["tent"], rewrite_counts={"tent": 3})
    out = app_graph.run_turn("alice", "t1", "optimize the tent article")
    assert out["reply"].startswith("Human review needed") and "rewritten 3 time" in out["reply"]
    assert not any(PLANNER in c["system"] for c in routed.calls)
    assert metrics.actions_for_user("alice")[0]["decision"] == "hold"


def test_optimize_button_targets_a_post_and_skips_the_routing_model(app_graph, routed):
    ids = seed(only=["boots", "espresso"])
    out = app_graph.run_turn("alice", "t1", "Optimise my article", action={"type": "optimize", "post_id": ids["boots"]})
    assert out["reply"].startswith("No change needed") and "Boots guide" in out["reply"]
    assert not any(SUPERVISOR in c["system"] for c in routed.calls)


def test_optimize_button_cannot_target_another_users_post(app_graph, routed):
    bob = seed("bob", only=["tent"])
    seed("alice", only=["boots"])
    out = app_graph.run_turn("alice", "t1", "Optimise", action={"type": "optimize", "post_id": bob["tent"]})
    assert "couldn't find that article" in out["reply"]
    assert metrics.actions_for_user("bob") == [] and metrics.actions_for_user("alice") == []


def test_cron_logs_decisions_for_every_user_and_skips_rank_checks_on_localhost(monkeypatch):
    seed("alice", only=["tent"])
    seed("bob", only=["boots"])
    from fastapi.testclient import TestClient
    from config import settings
    import main
    monkeypatch.setattr(settings, "cron_secret", "s3cret")
    with TestClient(main.app) as client:
        assert client.post("/cron/run").status_code == 401
        assert client.post("/cron/run", headers={"X-Cron-Secret": "wrong"}).status_code == 401
        r = client.post("/cron/run", headers={"X-Cron-Secret": "s3cret"})
    assert r.status_code == 200
    body = r.json()
    assert body["rank_tracker"]["checked"] == 0 and "not a public URL" in body["rank_tracker"]["skipped"]
    assert body["optimizer"]["users"] == 2 and body["optimizer"]["rewrite"] == 1
    assert metrics.actions_for_user("alice")[0]["notes"].startswith("[cron]")


def test_cron_is_disabled_with_the_default_secret():
    from fastapi.testclient import TestClient
    import main
    with TestClient(main.app) as client:
        assert client.post("/cron/run", headers={"X-Cron-Secret": "change-me"}).status_code == 503


def test_live_rank_check_records_positions(monkeypatch):
    import run_context
    from config import settings
    from tools import web
    ids = seed(only=["boots"])
    monkeypatch.setattr(settings, "public_site_url", "https://rankforge.example.org")
    monkeypatch.setattr(web, "web_search", lambda q, max_results=5: [
        {"url": "https://other.com/a", "title": "", "snippet": ""},
        {"url": "https://www.rankforge.example.org/posts/alice-boots", "title": "", "snippet": ""}])
    monkeypatch.setattr(metrics, "last_checked", lambda ids_: {})
    ctx = run_context.RunContext(run_id="r", user_id="cron", thread_id="cron")
    token = run_context.set_run(ctx)
    try:
        out = rank_tracker.track_live(limit=5)
    finally:
        run_context.reset_run(token)
    assert out == {"checked": 1, "ranked": 1, "day": TODAY.isoformat()}
    today = [r for r in metrics.rankings_for_user("alice") if r["date"] == TODAY and r["source"] == "tavily"]
    assert today and today[0]["position"] == 2 and today[0]["post_id"] == ids["boots"]
