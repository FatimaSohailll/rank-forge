"""Published posts, tracked affiliate links and click/view counters.

Postgres when available, in-process dicts otherwise (dev/tests only).
`create_post` etc. take user_id; the public read functions (`get_published`, `get_link`) are the
only unscoped reads, because anyone may read a published post or follow a tracked link.
"""
import itertools
from datetime import date, datetime, timezone

from psycopg.types.json import Jsonb

from stores import db

_posts: dict[int, dict] = {}
_links: dict[str, dict] = {}
_clicks: dict[tuple[int, date], dict] = {}
_ids = itertools.count(1)


def slug_exists(slug: str) -> bool:
    if db.enabled():
        with db.pool.connection() as conn:
            return conn.execute("select 1 from posts where slug=%s", (slug,)).fetchone() is not None
    return any(p["slug"] == slug for p in _posts.values())


def create_post(user_id: str, slug: str, title: str, keyword: str, meta_description: str, html: str,
                schema_jsonld: dict) -> dict:
    if db.enabled():
        with db.pool.connection() as conn:
            return conn.execute(
                "insert into posts (user_id, slug, title, keyword, meta_description, html, schema_jsonld)"
                " values (%s,%s,%s,%s,%s,%s,%s) returning id, slug, title, published_at",
                (user_id, slug, title, keyword, meta_description, html, Jsonb(schema_jsonld))).fetchone()
    pid = next(_ids)
    now = datetime.now(timezone.utc)
    _posts[pid] = {"id": pid, "user_id": user_id, "slug": slug, "title": title, "keyword": keyword,
                   "meta_description": meta_description, "html": html, "schema_jsonld": schema_jsonld,
                   "status": "published", "published_at": now, "updated_at": now}
    return {"id": pid, "slug": slug, "title": title, "published_at": now}


def get_published(slug: str) -> dict | None:
    if db.enabled():
        with db.pool.connection() as conn:
            return conn.execute(
                "select id, user_id, slug, title, keyword, meta_description, html, schema_jsonld, published_at,"
                " updated_at from posts where slug=%s and status='published'", (slug,)).fetchone()
    post = next((p for p in _posts.values() if p["slug"] == slug and p["status"] == "published"), None)
    return dict(post) if post else None


def list_published(limit: int = 500) -> list[dict]:
    if db.enabled():
        with db.pool.connection() as conn:
            return conn.execute("select slug, title, updated_at from posts where status='published'"
                                " order by published_at desc limit %s", (limit,)).fetchall()
    posts = sorted((p for p in _posts.values() if p["status"] == "published"),
                   key=lambda p: p["published_at"], reverse=True)
    return [{"slug": p["slug"], "title": p["title"], "updated_at": p["updated_at"]} for p in posts[:limit]]


def create_link(code: str, user_id: str, post_id: int, program_id: int | None, target_url: str,
                anchor_text: str) -> None:
    if db.enabled():
        with db.pool.connection() as conn:
            conn.execute("insert into affiliate_links (code, user_id, post_id, program_id, target_url, anchor_text)"
                         " values (%s,%s,%s,%s,%s,%s)", (code, user_id, post_id, program_id, target_url, anchor_text))
        return
    _links[code] = {"code": code, "user_id": user_id, "post_id": post_id, "program_id": program_id,
                    "target_url": target_url, "anchor_text": anchor_text}


def get_link(code: str) -> dict | None:
    if db.enabled():
        with db.pool.connection() as conn:
            return conn.execute("select code, user_id, post_id, target_url from affiliate_links where code=%s",
                                (code,)).fetchone()
    link = _links.get(code)
    return dict(link) if link else None


def _bump(user_id: str, post_id: int, column: str) -> None:
    """Add one to today's counter for the post. `column` is a fixed internal name, never user input."""
    assert column in ("affiliate_clicks", "sessions")
    if db.enabled():
        with db.pool.connection() as conn:
            conn.execute(
                f"insert into clicks (user_id, post_id, date, {column}) values (%s,%s,current_date,1)"
                f" on conflict (post_id, date) do update set {column} = clicks.{column} + 1", (user_id, post_id))
        return
    row = _clicks.setdefault((post_id, date.today()), {"user_id": user_id, "sessions": 0, "affiliate_clicks": 0})
    row[column] += 1


def record_click(user_id: str, post_id: int) -> None:
    _bump(user_id, post_id, "affiliate_clicks")


def record_view(user_id: str, post_id: int) -> None:
    _bump(user_id, post_id, "sessions")


def today_counts(post_id: int) -> dict:
    if db.enabled():
        with db.pool.connection() as conn:
            row = conn.execute("select sessions, affiliate_clicks from clicks where post_id=%s and date=current_date",
                               (post_id,)).fetchone()
        return row or {"sessions": 0, "affiliate_clicks": 0}
    row = _clicks.get((post_id, date.today()), {})
    return {"sessions": row.get("sessions", 0), "affiliate_clicks": row.get("affiliate_clicks", 0)}


def clear_memory() -> None:
    """Test helper for the in-process fallback."""
    _posts.clear()
    _links.clear()
    _clicks.clear()
