"""Content briefs per user. Postgres when available, in-process list otherwise (dev/tests only)."""
import itertools

from psycopg.types.json import Jsonb

from stores import db

_mem: list[dict] = []
_ids = itertools.count(1)


def save_brief(user_id: str, keyword: str, brief: dict) -> int:
    """Store a brief and return its id."""
    if db.enabled():
        with db.pool.connection() as conn:
            row = conn.execute(
                "insert into briefs (user_id, keyword, brief) values (%s,%s,%s) returning id",
                (user_id, keyword, Jsonb(brief)),
            ).fetchone()
            return row["id"]
    bid = next(_ids)
    _mem.append({"id": bid, "user_id": user_id, "keyword": keyword, "brief": brief})
    return bid


def latest_brief(user_id: str, keyword: str | None = None) -> dict | None:
    if db.enabled():
        sql = "select id, keyword, brief from briefs where user_id=%s"
        params: list = [user_id]
        if keyword:
            sql += " and keyword=%s"
            params.append(keyword)
        with db.pool.connection() as conn:
            return conn.execute(sql + " order by id desc limit 1", params).fetchone()
    mine = [b for b in _mem if b["user_id"] == user_id and (keyword is None or b["keyword"] == keyword)]
    return ({"id": mine[-1]["id"], "keyword": mine[-1]["keyword"], "brief": mine[-1]["brief"]}
            if mine else None)


def clear_memory() -> None:
    """Test helper for the in-process fallback."""
    _mem.clear()
