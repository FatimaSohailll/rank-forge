"""Keyword Scout: finds keyword ideas, scores them, and picks the target.

Deterministic scoring (no LLM) keeps cost near zero. Every number is an ESTIMATE:
- demand / intent come from Google Autocomplete (see tools/keywords.py)
- competition comes from how many top Tavily results are big authority sites
- commission comes from the best matching affiliate program (needs the database)
"""
import logging
from urllib.parse import urlparse

import llm
from state import RankForgeState
from stores import affiliate, keywords as keyword_store
from tools import ToolUnavailable
from tools import keywords as keyword_tool
from tools import web

log = logging.getLogger("rankforge.scout")

AUTHORITY_DOMAINS = {
    "amazon.com", "wikipedia.org", "youtube.com", "reddit.com", "nytimes.com", "forbes.com",
    "pcmag.com", "techradar.com", "cnet.com", "rtings.com", "tomsguide.com", "wired.com",
    "healthline.com", "webmd.com", "rei.com", "bestbuy.com", "walmart.com", "pinterest.com",
}
COMPETITION_CHECKS = 3          # Tavily credits are limited: only check the top ideas
DEFAULT_COMPETITION = 0.5       # used when competition was not checked
WEIGHTS = {"demand": 0.35, "intent": 0.30, "open": 0.25, "commission": 0.10}


def _domain(url: str) -> str:
    host = (urlparse(url).hostname or "").lower()
    return host[4:] if host.startswith("www.") else host


def competition_for(keyword: str) -> float | None:
    """0..1: share of the top results held by big authority sites. None if search is unavailable."""
    try:
        results = web.web_search(keyword, max_results=5)
    except ToolUnavailable:
        return None
    if not results:
        return DEFAULT_COMPETITION
    strong = sum(1 for r in results if any(_domain(r["url"]).endswith(d) for d in AUTHORITY_DOMAINS))
    return round(0.2 + 0.8 * strong / len(results), 2)


def _commission(topic: str, programs: list[dict]) -> tuple[float, str]:
    """Best commission % and category for a topic. One tiny LLM call, only if programs exist."""
    categories = sorted({p["category"] for p in programs})
    if not categories:
        return 0.0, ""
    schema = {"type": "object", "properties": {"category": {"type": "string", "enum": categories + ["none"]}},
              "required": ["category"], "additionalProperties": False}
    try:
        out = llm.complete("Pick the product category that best fits this search topic, or 'none'.",
                           [{"role": "user", "content": topic}], max_tokens=30, schema=schema)
        category = out.get("category", "none")
    except Exception:
        log.warning("category match failed", exc_info=True)
        return 0.0, ""
    pcts = [float(p["commission_pct"]) for p in programs if p["category"] == category]
    return (max(pcts), category) if pcts else (0.0, "")


def score(meta: dict) -> float:
    open_ = 1.0 - (meta.get("competition") if meta.get("competition") is not None else DEFAULT_COMPETITION)
    commission = min(1.0, (meta.get("commission") or 0.0) / 10.0)
    total = (WEIGHTS["demand"] * meta.get("demand", 0.0) + WEIGHTS["intent"] * meta.get("intent", 0.0)
             + WEIGHTS["open"] * open_ + WEIGHTS["commission"] * commission)
    return round(total, 3)


def _format_reply(chosen: str, ranked: list[dict]) -> str:
    lines = [f"Top keyword ideas (scores are estimates, not search volumes):"]
    for i, k in enumerate(ranked[:5], 1):
        comp = "n/a" if k.get("competition") is None else f"{k['competition']:.2f}"
        lines.append(f"{i}. **{k['keyword']}**: score {k['score']:.2f} (demand {k['demand']:.2f}, "
                     f"buyer intent {k['intent']:.2f}, competition {comp})")
    lines.append(f"\nBest target: **{chosen}**. Ask me to write an article on it, or pick another.")
    return "\n".join(lines)


def keyword_scout(state: RankForgeState) -> dict:
    topic = (state.get("topic") or "").strip()
    location = (state.get("location") or "").strip() or (state.get("profile") or {}).get("location") or None
    user_id = state["user_id"]
    writing = state.get("route") == "write_article"
    if not topic:
        return {"reply": "Which topic or niche should I research keywords for?"}

    try:
        ideas = keyword_tool.keyword_search(topic, location)
    except ToolUnavailable:
        ideas = []
        degraded = True
    else:
        degraded = False

    programs = affiliate.list_programs()
    commission, category = _commission(topic, programs)

    # The user's own topic is always a candidate (and the target when they asked for an article).
    if not any(i["keyword"] == topic.lower() for i in ideas):
        ideas.append({"keyword": topic.lower(), "demand": 0.5, "intent": keyword_tool.intent_score(topic),
                      "source": "user"})
    for i in ideas:
        i["commission"], i["category"], i["competition"] = commission, category, None

    ideas.sort(key=lambda i: i["demand"] + i["intent"], reverse=True)
    if writing:
        # Writing an article: only the user's own topic matters, so spend a single search on it.
        to_check = [next(i for i in ideas if i["keyword"] == topic.lower())]
    else:
        to_check = ideas[:COMPETITION_CHECKS]
    for i in to_check:
        i["competition"] = competition_for(i["keyword"])
    for i in ideas:
        i["score"] = score(i)
    ranked = sorted(ideas, key=lambda i: i["score"], reverse=True)

    keyword_store.save_keywords(user_id, ranked[:10])

    chosen = topic.lower() if writing else ranked[0]["keyword"]
    meta = next(i for i in ranked if i["keyword"] == chosen)
    meta = {**meta, "estimate": True}

    out: dict = {"keyword": chosen, "keyword_meta": meta, "keyword_candidates": ranked[:5],
                 "degraded": degraded}
    if not writing:
        out["reply"] = _format_reply(chosen, ranked)
        if degraded:
            out["reply"] = ("Keyword suggestions are unavailable right now, so this is based only on "
                            "your topic.\n\n") + out["reply"]
        if state.get("route") == "optimize" and state.get("optimizer_notes"):
            out["reply"] = (f"Re-target: {state['optimizer_notes']}\nHere are alternative keywords to aim "
                            "for instead:\n\n") + out["reply"]
    return out
