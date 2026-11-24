"""Keyword ideas from Google Autocomplete (free, no key).

Autocomplete has no search volume, so `demand` is an ESTIMATE: how early and how often a
phrase shows up across several related queries. It ranks ideas against each other; it is
not a monthly search count and must be presented as an estimate.
"""
import logging

import httpx

from tools import ToolUnavailable, log_call

log = logging.getLogger("rankforge.tools.keywords")

AUTOCOMPLETE_URL = "https://suggestqueries.google.com/complete/search"
TIMEOUT = 10.0

COUNTRIES = {
    "us": "us", "usa": "us", "united states": "us",
    "uk": "gb", "gb": "gb", "united kingdom": "gb",
    "pakistan": "pk", "pk": "pk", "india": "in", "in": "in",
    "canada": "ca", "ca": "ca", "australia": "au", "au": "au",
    "germany": "de", "de": "de", "uae": "ae", "ae": "ae",
}

# Words that signal someone is comparing or about to buy.
INTENT_WORDS = {"best", "top", "review", "reviews", "vs", "versus", "buy", "price", "cheap",
                "cheapest", "deal", "deals", "discount", "compare", "comparison", "alternative",
                "alternatives", "under", "worth"}


def country_code(location: str | None) -> str:
    return COUNTRIES.get((location or "").strip().lower(), "us")


def _suggest(query: str, gl: str) -> list[str]:
    try:
        r = httpx.get(AUTOCOMPLETE_URL, params={"client": "firefox", "q": query, "gl": gl, "hl": "en"},
                      timeout=TIMEOUT)
        r.raise_for_status()
        data = r.json()
        return [s for s in data[1] if isinstance(s, str)]
    except (httpx.HTTPError, ValueError, IndexError, TypeError) as e:
        log.warning("autocomplete failed for %r: %s", query, e)
        raise ToolUnavailable(f"autocomplete failed: {e.__class__.__name__}") from e


def intent_score(keyword: str) -> float:
    """0..1 buyer-intent estimate from commercial words in the phrase."""
    hits = len(set(keyword.lower().split()) & INTENT_WORDS)
    return round(min(1.0, 0.1 + 0.3 * hits), 2)


def keyword_search(topic: str, location: str | None = None, limit: int = 20) -> list[dict]:
    """Return [{keyword, demand, intent, source}] sorted by demand. demand is an estimate in 0..1."""
    topic = topic.strip()
    log_call("keyword_search", {"topic": topic, "location": location})
    gl = country_code(location)
    queries = [topic, f"best {topic}", f"{topic} for", f"{topic} vs", f"{topic} review"]

    weights: dict[str, float] = {}
    failures = 0
    for q_index, q in enumerate(queries):
        try:
            suggestions = _suggest(q, gl)
        except ToolUnavailable:
            failures += 1
            continue
        query_weight = 1.0 if q_index == 0 else 0.6
        for rank, phrase in enumerate(suggestions):
            phrase = phrase.strip().lower()
            if phrase and phrase != topic.lower():
                weights[phrase] = weights.get(phrase, 0.0) + query_weight * (1.0 - rank / 10)
    if failures == len(queries):
        raise ToolUnavailable("autocomplete unavailable")

    top = max(weights.values(), default=1.0)
    ideas = [
        {"keyword": k, "demand": round(w / top, 2), "intent": intent_score(k), "source": "autocomplete"}
        for k, w in weights.items()
    ]
    ideas.sort(key=lambda i: (i["demand"], i["intent"]), reverse=True)
    return ideas[:limit]
