"""Scored keywords per user. Postgres when available, in-process dict otherwise (dev/tests only).

Every function takes user_id and filters by it.
"""
from stores import db

_mem: dict[str, dict[str, dict]] = {}

COLUMNS = ("keyword", "demand", "competition", "intent", "commission", "score", "status")


def save_keywords(user_id: str, rows: list[dict]) -> None:
    """Upsert scored keywords. An existing row keeps its status."""
    if db.enabled():
        with db.pool.connection() as conn:
            for r in rows:
                conn.execute(
                    "insert into keywords (user_id, keyword, demand, competition, intent, commission, score)"
                    " values (%s,%s,%s,%s,%s,%s,%s)"
                    " on conflict (user_id, keyword) do update set demand=excluded.demand,"
                    " competition=excluded.competition, intent=excluded.intent,"
                    " commission=excluded.commission, score=excluded.score",
                    (user_id, r["keyword"], r.get("demand"), r.get("competition"), r.get("intent"),
                     r.get("commission"), r.get("score")),
                )
        return
    bucket = _mem.setdefault(user_id, {})
    for r in rows:
        old = bucket.get(r["keyword"], {})
        bucket[r["keyword"]] = {**{c: r.get(c) for c in COLUMNS}, "status": old.get("status", "new")}


def list_keywords(user_id: str, limit: int = 20) -> list[dict]:
    if db.enabled():
        with db.pool.connection() as conn:
            return conn.execute(
                "select keyword, demand, competition, intent, commission, score, status"
                " from keywords where user_id=%s order by score desc nulls last limit %s",
                (user_id, limit),
            ).fetchall()
    rows = sorted(_mem.get(user_id, {}).values(), key=lambda r: r.get("score") or 0, reverse=True)
    return rows[:limit]


def clear_memory() -> None:
    """Test helper for the in-process fallback."""
    _mem.clear()
