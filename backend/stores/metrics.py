"""Monitor time series and the Optimizer audit trail (tables `rankings`, `clicks`, `action_log`).

Rank Tracker writes `rankings`; views and /r/{code} clicks write `clicks` (stores/posts.py);
the Optimizer reads both and writes `action_log`. Postgres when available, in-process otherwise.
Every query filters by user_id.
"""
import itertools
from datetime import date, datetime, timedelta, timezone

from stores import db

_rankings: dict[tuple[int, str, date], dict] = {}
_actions: list[dict] = []
_ids = itertools.count(1)


# ---------- rankings ----------

def add_ranking(user_id: str, post_id: int, keyword: str, position: int, source: str,
                day: date | None = None) -> None:
    day = day or date.today()
    if db.enabled():
        with db.pool.connection() as conn:
            conn.execute(
                "insert into rankings (user_id, post_id, keyword, position, source, date) values (%s,%s,%s,%s,%s,%s)"
                " on conflict (post_id, keyword, date) do update set position=excluded.position,"
                " source=excluded.source", (user_id, post_id, keyword, position, source, day))
        return
    _rankings[(post_id, keyword, day)] = {"user_id": user_id, "post_id": post_id, "keyword": keyword,
                                          "position": position, "source": source, "date": day}


def rankings_for_user(user_id: str, days: int = 30) -> list[dict]:
    """[{post_id, keyword, position, source, date}] oldest first."""
    since = date.today() - timedelta(days=days - 1)
    if db.enabled():
        with db.pool.connection() as conn:
            return conn.execute(
                "select post_id, keyword, position, source, date from rankings"
                " where user_id=%s and date >= %s order by date", (user_id, since)).fetchall()
    return sorted((dict(r) for r in _rankings.values() if r["user_id"] == user_id and r["date"] >= since),
                  key=lambda r: r["date"])


def last_checked(post_ids: list[int]) -> dict[int, date]:
    """Most recent ranking date per post (any source), for the cron's oldest-first ordering."""
    if not post_ids:
        return {}
    if db.enabled():
        with db.pool.connection() as conn:
            rows = conn.execute("select post_id, max(date) as d from rankings where post_id = any(%s)"
                                " group by post_id", (post_ids,)).fetchall()
        return {r["post_id"]: r["d"] for r in rows}
    out: dict[int, date] = {}
    for r in _rankings.values():
        if r["post_id"] in post_ids:
            out[r["post_id"]] = max(out.get(r["post_id"], r["date"]), r["date"])
    return out


# ---------- clicks (written by stores/posts.py) ----------

def clicks_for_user(user_id: str, days: int = 30) -> list[dict]:
    """[{post_id, date, sessions, affiliate_clicks, conversions}] oldest first."""
    since = date.today() - timedelta(days=days - 1)
    if db.enabled():
        with db.pool.connection() as conn:
            return conn.execute(
                "select post_id, date, sessions, affiliate_clicks, conversions from clicks"
                " where user_id=%s and date >= %s order by date", (user_id, since)).fetchall()
    from stores import posts
    rows = [{"post_id": pid, "date": day, "sessions": r.get("sessions", 0),
             "affiliate_clicks": r.get("affiliate_clicks", 0), "conversions": r.get("conversions", 0)}
            for (pid, day), r in posts._clicks.items() if r["user_id"] == user_id and day >= since]
    return sorted(rows, key=lambda r: r["date"])


def add_clicks(user_id: str, post_id: int, day: date, sessions: int, affiliate_clicks: int,
               conversions: int = 0) -> None:
    """Write one day of click history (seeding and tests; live traffic goes through posts.record_*)."""
    if db.enabled():
        with db.pool.connection() as conn:
            conn.execute(
                "insert into clicks (user_id, post_id, date, sessions, affiliate_clicks, conversions)"
                " values (%s,%s,%s,%s,%s,%s) on conflict (post_id, date) do update set sessions=excluded.sessions,"
                " affiliate_clicks=excluded.affiliate_clicks, conversions=excluded.conversions",
                (user_id, post_id, day, sessions, affiliate_clicks, conversions))
        return
    from stores import posts
    posts._clicks[(post_id, day)] = {"user_id": user_id, "sessions": sessions,
                                     "affiliate_clicks": affiliate_clicks, "conversions": conversions}


# ---------- action log ----------

def log_action(user_id: str, post_id: int | None, decision: str, notes: str, loop_count: int) -> None:
    assert decision in ("rewrite", "retarget", "hold")
    if db.enabled():
        with db.pool.connection() as conn:
            conn.execute("insert into action_log (user_id, post_id, decision, notes, loop_count)"
                         " values (%s,%s,%s,%s,%s)", (user_id, post_id, decision, notes[:1000], loop_count))
        return
    _actions.append({"id": next(_ids), "user_id": user_id, "post_id": post_id, "decision": decision,
                     "notes": notes[:1000], "loop_count": loop_count, "created_at": datetime.now(timezone.utc)})


def actions_for_user(user_id: str, limit: int = 20) -> list[dict]:
    if db.enabled():
        with db.pool.connection() as conn:
            return conn.execute(
                "select a.id, a.post_id, p.title, a.decision, a.notes, a.loop_count, a.created_at"
                " from action_log a left join posts p on p.id=a.post_id"
                " where a.user_id=%s order by a.id desc limit %s", (user_id, limit)).fetchall()
    from stores import posts
    out = []
    for a in reversed(_actions):
        if a["user_id"] == user_id:
            post = posts._posts.get(a["post_id"]) if a["post_id"] else None
            out.append({**a, "title": post["title"] if post else None})
    return out[:limit]


def clear_memory() -> None:
    """Test helper for the in-process fallback."""
    _rankings.clear()
    _actions.clear()
