"""Rank Tracker (Monitor lane, runs in parallel with the Click Tracker).

In the chat graph it only READS the rankings time series (no search credits) and summarises it per post.
The cron job (`track_live`, called from POST /cron/run) is what spends credits: it checks the live SERP
position of a few published posts per run, oldest-checked first, and writes it to `rankings`.
"""
import logging
from datetime import date
from urllib.parse import urlparse

from config import settings
from state import RankForgeState
from stores import metrics, posts
from tools import ToolUnavailable, serp

log = logging.getLogger("rankforge.rank_tracker")

NOT_RANKED = 100          # stored when the post is not in the top results checked
RECENT_DAYS = 7
PRIOR_DAYS = (8, 21)      # compare the last week against days 8-21 ago


def _avg(values: list[float]) -> float | None:
    return round(sum(values) / len(values), 1) if values else None


def summarise(rows: list[dict], today: date | None = None) -> list[dict]:
    """Per-post summary of ranking rows: [{post_id, keyword, recent_position, prior_position, days_tracked, source}]."""
    today = today or date.today()
    by_post: dict[int, list[dict]] = {}
    for r in rows:
        by_post.setdefault(r["post_id"], []).append(r)
    out = []
    for post_id, rs in by_post.items():
        def window(lo: int, hi: int) -> list[float]:
            return [r["position"] for r in rs if lo <= (today - _as_date(r["date"])).days <= hi]
        out.append({"post_id": post_id, "keyword": rs[-1]["keyword"],
                    "recent_position": _avg(window(0, RECENT_DAYS - 1)),
                    "prior_position": _avg(window(*PRIOR_DAYS)),
                    "days_tracked": len({_as_date(r["date"]) for r in rs}),
                    "source": rs[-1].get("source", "")})
    return out


def _as_date(value) -> date:
    return value if isinstance(value, date) else date.fromisoformat(str(value)[:10])


def rank_tracker(state: RankForgeState) -> dict:
    return {"rankings": summarise(metrics.rankings_for_user(state["user_id"]))}


def _is_public(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    return bool(host) and host not in ("localhost", "127.0.0.1", "0.0.0.0") and not host.endswith(".local")


def track_live(limit: int | None = None) -> dict:
    """Cron: check live SERP positions for up to `limit` published posts. Needs an active RunContext."""
    limit = settings.rank_checks_per_cron if limit is None else limit
    if not _is_public(settings.public_site_url):
        return {"checked": 0, "skipped": "PUBLIC_SITE_URL is not a public URL, so posts cannot rank yet"}
    published = [p for p in posts.list_all() if p["status"] == "published"]
    last = metrics.last_checked([p["id"] for p in published])
    published.sort(key=lambda p: last.get(p["id"], date.min))           # least recently checked first
    checked, found = 0, 0
    for p in published[:limit]:
        if last.get(p["id"]) == date.today():
            continue
        try:
            position = serp.serp_position(p["keyword"], settings.public_site_url)
        except ToolUnavailable as e:
            log.warning("rank check stopped: %s", e)
            break
        metrics.add_ranking(p["user_id"], p["id"], p["keyword"], position or NOT_RANKED, "tavily")
        checked += 1
        found += position is not None
    return {"checked": checked, "ranked": found, "day": date.today().isoformat()}
