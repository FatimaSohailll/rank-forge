"""Click Tracker (Monitor lane, runs in parallel with the Rank Tracker).

Reads the clicks time series. Live data comes from our own site: every article view adds a session and
every /r/{code} redirect adds an affiliate click (stores/posts.py), so no third-party analytics key is
needed. Seeded demo history is labelled simulated in db/seed.sql.
"""
from datetime import date

from state import RankForgeState
from stores import metrics


def _as_date(value) -> date:
    return value if isinstance(value, date) else date.fromisoformat(str(value)[:10])


def summarise(rows: list[dict], today: date | None = None) -> list[dict]:
    """[{post_id, sessions_7d, sessions_prev_7d, affiliate_clicks_7d, conversions_7d, ctr_7d, sessions_30d}]."""
    today = today or date.today()
    by_post: dict[int, dict] = {}
    for r in rows:
        age = (today - _as_date(r["date"])).days
        s = by_post.setdefault(r["post_id"], {"post_id": r["post_id"], "sessions_7d": 0, "sessions_prev_7d": 0,
                                              "affiliate_clicks_7d": 0, "conversions_7d": 0, "sessions_30d": 0})
        sessions = int(r.get("sessions") or 0)
        s["sessions_30d"] += sessions
        if age < 7:
            s["sessions_7d"] += sessions
            s["affiliate_clicks_7d"] += int(r.get("affiliate_clicks") or 0)
            s["conversions_7d"] += int(r.get("conversions") or 0)
        elif age < 14:
            s["sessions_prev_7d"] += sessions
    for s in by_post.values():
        s["ctr_7d"] = round(s["affiliate_clicks_7d"] / s["sessions_7d"], 4) if s["sessions_7d"] else 0.0
    return list(by_post.values())


def click_tracker(state: RankForgeState) -> dict:
    return {"clicks": summarise(metrics.clicks_for_user(state["user_id"]))}
