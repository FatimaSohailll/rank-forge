"""Article drafts per user and stage (draft | linked | final).

Postgres when available, in-process list otherwise (dev/tests only). Every query filters by user_id.
"""
import itertools

from psycopg.types.json import Jsonb

from stores import db

_mem: list[dict] = []
_ids = itertools.count(1)


def save_draft(user_id: str, brief_id: int | None, stage: str, body: str,
               flagged_claims: list[dict] | None = None, seo_report: dict | None = None) -> int:
    flagged_claims = flagged_claims or []
    if db.enabled():
        with db.pool.connection() as conn:
            row = conn.execute(
                "insert into drafts (user_id, brief_id, stage, body, flagged_claims, seo_report)"
                " values (%s,%s,%s,%s,%s,%s) returning id",
                (user_id, brief_id, stage, body, Jsonb(flagged_claims),
                 Jsonb(seo_report) if seo_report is not None else None),
            ).fetchone()
            return row["id"]
    did = next(_ids)
    _mem.append({"id": did, "user_id": user_id, "brief_id": brief_id, "stage": stage, "body": body,
                 "flagged_claims": flagged_claims, "seo_report": seo_report})
    return did


def get_draft(user_id: str, draft_id: int) -> dict | None:
    """One draft by id, only if it belongs to user_id."""
    if db.enabled():
        with db.pool.connection() as conn:
            return conn.execute(
                "select id, brief_id, stage, body, flagged_claims, seo_report from drafts where user_id=%s and id=%s",
                (user_id, draft_id)).fetchone()
    return next((dict(d) for d in _mem if d["user_id"] == user_id and d["id"] == draft_id), None)


def set_stage(user_id: str, draft_id: int, stage: str) -> None:
    """Move a draft to another stage (e.g. 'published'), only if it belongs to user_id."""
    if db.enabled():
        with db.pool.connection() as conn:
            conn.execute("update drafts set stage=%s where user_id=%s and id=%s", (stage, user_id, draft_id))
        return
    for d in _mem:
        if d["user_id"] == user_id and d["id"] == draft_id:
            d["stage"] = stage


def latest_draft(user_id: str, stage: str | None = None) -> dict | None:
    if db.enabled():
        sql = "select id, brief_id, stage, body, flagged_claims, seo_report from drafts where user_id=%s"
        params: list = [user_id]
        if stage:
            sql += " and stage=%s"
            params.append(stage)
        with db.pool.connection() as conn:
            return conn.execute(sql + " order by id desc limit 1", params).fetchone()
    mine = [d for d in _mem if d["user_id"] == user_id and (stage is None or d["stage"] == stage)]
    return dict(mine[-1]) if mine else None


def clear_memory() -> None:
    """Test helper for the in-process fallback."""
    _mem.clear()
