"""Human-in-the-loop approvals (table `approvals`).

An approval is created when the graph pauses on a risky action (publishing). It is bound to one
user, one thread and one action payload, carries a random resume token, and can be resolved exactly
once. Postgres when available, in-process list otherwise (dev/tests only).
"""
import itertools
import secrets
from datetime import datetime, timezone

from psycopg.types.json import Jsonb

from stores import db

STATUSES = {"approved", "rejected", "amended"}
_mem: list[dict] = []
_ids = itertools.count(1)


def create(user_id: str, thread_key: str, trigger: str, action: dict) -> dict:
    """Open a pending approval. Returns {id, resume_token, ...}."""
    token = secrets.token_urlsafe(24)
    if db.enabled():
        with db.pool.connection() as conn:
            return conn.execute(
                "insert into approvals (user_id, thread_key, resume_token, trigger, action)"
                " values (%s,%s,%s,%s,%s)"
                " returning id, user_id, thread_key, resume_token, trigger, action, status, created_at",
                (user_id, thread_key, token, trigger, Jsonb(action)),
            ).fetchone()
    row = {"id": next(_ids), "user_id": user_id, "thread_key": thread_key, "resume_token": token,
           "trigger": trigger, "action": action, "status": "pending",
           "created_at": datetime.now(timezone.utc), "resolved_at": None}
    _mem.append(row)
    return dict(row)


def pending_for_thread(user_id: str, thread_key: str) -> dict | None:
    """The newest pending approval for this user's thread, if any."""
    if db.enabled():
        with db.pool.connection() as conn:
            return conn.execute(
                "select id, user_id, thread_key, resume_token, trigger, action, status, created_at"
                " from approvals where user_id=%s and thread_key=%s and status='pending'"
                " order by id desc limit 1", (user_id, thread_key)).fetchone()
    rows = [r for r in _mem if r["user_id"] == user_id and r["thread_key"] == thread_key and r["status"] == "pending"]
    return dict(rows[-1]) if rows else None


def list_pending(user_id: str) -> list[dict]:
    if db.enabled():
        with db.pool.connection() as conn:
            return conn.execute(
                "select id, thread_key, resume_token, trigger, action, status, created_at"
                " from approvals where user_id=%s and status='pending' order by id desc", (user_id,)).fetchall()
    return [dict(r) for r in reversed(_mem) if r["user_id"] == user_id and r["status"] == "pending"]


def resolve(user_id: str, token: str, status: str) -> dict | None:
    """Atomically move a pending approval (owned by user_id) to approved/rejected/amended.

    Returns the approval, or None if the token is unknown, belongs to someone else, or was already
    resolved. Because the update only matches status='pending', an approval can never run twice.
    """
    if status not in STATUSES:
        raise ValueError(f"bad status {status!r}")
    if db.enabled():
        with db.pool.connection() as conn:
            return conn.execute(
                "update approvals set status=%s, resolved_at=now()"
                " where user_id=%s and resume_token=%s and status='pending'"
                " returning id, user_id, thread_key, resume_token, trigger, action, status", (status, user_id, token)
            ).fetchone()
    for r in _mem:
        if r["user_id"] == user_id and r["resume_token"] == token and r["status"] == "pending":
            r["status"], r["resolved_at"] = status, datetime.now(timezone.utc)
            return dict(r)
    return None


def clear_memory() -> None:
    """Test helper for the in-process fallback."""
    _mem.clear()
