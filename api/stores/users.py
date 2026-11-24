"""Accounts (table `users`). Postgres when available, in-process dict otherwise (dev/tests only).

A user with a password_hash is a login account: the API only acts as that user with a valid session.
A user without one is an API-only user (created on first /chat contact, e.g. by the benchmark).
"""
from datetime import datetime, timezone

from stores import db

_mem: dict[str, dict] = {}

COLUMNS = "user_id, display_name, is_admin, password_hash, disabled, created_at"


def get(user_id: str) -> dict | None:
    if db.enabled():
        with db.pool.connection() as conn:
            return conn.execute(f"select {COLUMNS} from users where user_id=%s", (user_id,)).fetchone()
    row = _mem.get(user_id)
    return dict(row) if row else None


def ensure(user_id: str) -> None:
    """Create an API-only user row on first contact (no password)."""
    if db.enabled():
        db.ensure_user(user_id)
        return
    _mem.setdefault(user_id, {"user_id": user_id, "display_name": None, "is_admin": False,
                              "password_hash": None, "disabled": False,
                              "created_at": datetime.now(timezone.utc)})


def create_account(user_id: str, password_hash: str, display_name: str = "", is_admin: bool = False) -> bool:
    """Create a login account. False if the id is taken (by an account or an API-only user)."""
    if db.enabled():
        with db.pool.connection() as conn:
            row = conn.execute(
                "insert into users (user_id, display_name, is_admin, password_hash) values (%s,%s,%s,%s)"
                " on conflict (user_id) do nothing returning user_id",
                (user_id, display_name or None, is_admin, password_hash)).fetchone()
        return row is not None
    if user_id in _mem:
        return False
    _mem[user_id] = {"user_id": user_id, "display_name": display_name or None, "is_admin": is_admin,
                     "password_hash": password_hash, "disabled": False, "created_at": datetime.now(timezone.utc)}
    return True


def set_password(user_id: str, password_hash: str, is_admin: bool | None = None) -> None:
    """Create or update an account's password (used for the bootstrap admin and demo users)."""
    if db.enabled():
        with db.pool.connection() as conn:
            conn.execute(
                "insert into users (user_id, password_hash, is_admin) values (%s,%s,%s)"
                " on conflict (user_id) do update set password_hash=excluded.password_hash,"
                " is_admin=coalesce(%s, users.is_admin)",
                (user_id, password_hash, bool(is_admin), is_admin))
        return
    ensure(user_id)
    _mem[user_id]["password_hash"] = password_hash
    if is_admin is not None:
        _mem[user_id]["is_admin"] = is_admin


def set_disabled(user_id: str, disabled: bool) -> None:
    if db.enabled():
        with db.pool.connection() as conn:
            conn.execute("update users set disabled=%s where user_id=%s and not is_admin", (disabled, user_id))
        return
    if user_id in _mem and not _mem[user_id]["is_admin"]:
        _mem[user_id]["disabled"] = disabled


def list_with_stats() -> list[dict]:
    """Admin: every user with post count, 30-day LLM spend and request count."""
    if db.enabled():
        with db.pool.connection() as conn:
            return conn.execute(
                "select u.user_id, u.display_name, u.is_admin, u.disabled, u.created_at,"
                " (u.password_hash is not null) as has_login,"
                " (select count(*) from posts p where p.user_id=u.user_id and p.status='published') as posts,"
                " (select count(*) from drafts d where d.user_id=u.user_id and d.stage='final') as drafts,"
                " (select coalesce(sum(s.cost_usd),0) from spans s where s.user_id=u.user_id"
                "    and s.created_at > now() - interval '30 days') as spend_30d,"
                " (select count(distinct s.run_id) from spans s where s.user_id=u.user_id"
                "    and s.created_at > now() - interval '30 days') as runs_30d"
                " from users u order by u.created_at").fetchall()
    from stores import drafts, posts, spans
    out = []
    for u in _mem.values():
        mine = [s for s in spans.recent(10_000) if s["user_id"] == u["user_id"]]
        out.append({**{k: u[k] for k in ("user_id", "display_name", "is_admin", "disabled", "created_at")},
                    "has_login": bool(u["password_hash"]),
                    "posts": sum(1 for p in posts.list_for_user(u["user_id"]) if p["status"] == "published"),
                    "drafts": len(drafts.list_drafts(u["user_id"], "final")),
                    "spend_30d": round(sum(s["cost_usd"] for s in mine), 6),
                    "runs_30d": len({s["run_id"] for s in mine})})
    return out


def clear_memory() -> None:
    """Test helper for the in-process fallback."""
    _mem.clear()
