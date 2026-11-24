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


class SlugTaken(Exception):
    """Another post took the slug between the existence check and the insert."""


def create_post(user_id: str, slug: str, title: str, keyword: str, meta_description: str, html: str,
                schema_jsonld: dict) -> dict:
    if db.enabled():
        import psycopg
        try:
            with db.pool.connection() as conn:
                return conn.execute(
                    "insert into posts (user_id, slug, title, keyword, meta_description, html, schema_jsonld)"
                    " values (%s,%s,%s,%s,%s,%s,%s) returning id, slug, title, published_at",
                    (user_id, slug, title, keyword, meta_description, html, Jsonb(schema_jsonld))).fetchone()
        except psycopg.errors.UniqueViolation as e:
            raise SlugTaken(slug) from e
    if any(p["slug"] == slug for p in _posts.values()):
        raise SlugTaken(slug)
    pid = next(_ids)
    now = datetime.now(timezone.utc)
    _posts[pid] = {"id": pid, "user_id": user_id, "slug": slug, "title": title, "keyword": keyword,
                   "meta_description": meta_description, "html": html, "schema_jsonld": schema_jsonld,
                   "status": "published", "rewrite_count": 0, "published_at": now, "updated_at": now}
    return {"id": pid, "slug": slug, "title": title, "published_at": now}


def update_post(user_id: str, post_id: int, title: str, meta_description: str, html: str,
                schema_jsonld: dict) -> dict | None:
    """Replace a live post's body (Optimizer re-write loop). Keeps the slug; bumps rewrite_count."""
    if db.enabled():
        with db.pool.connection() as conn:
            return conn.execute(
                "update posts set title=%s, meta_description=%s, html=%s, schema_jsonld=%s,"
                " rewrite_count=rewrite_count+1, updated_at=now()"
                " where user_id=%s and id=%s returning id, slug, title, published_at, rewrite_count",
                (title, meta_description, html, Jsonb(schema_jsonld), user_id, post_id)).fetchone()
    post = _posts.get(post_id)
    if not post or post["user_id"] != user_id:
        return None
    post.update(title=title, meta_description=meta_description, html=html, schema_jsonld=schema_jsonld,
                rewrite_count=post.get("rewrite_count", 0) + 1, updated_at=datetime.now(timezone.utc))
    return {k: post[k] for k in ("id", "slug", "title", "published_at", "rewrite_count")}


_LIST_COLS = "id, user_id, slug, title, keyword, status, rewrite_count, published_at, updated_at"


def get_for_user(user_id: str, post_id: int) -> dict | None:
    """One post (any status) only if it belongs to user_id."""
    if db.enabled():
        with db.pool.connection() as conn:
            return conn.execute(f"select {_LIST_COLS}, meta_description from posts where user_id=%s and id=%s",
                                (user_id, post_id)).fetchone()
    post = _posts.get(post_id)
    return dict(post) if post and post["user_id"] == user_id else None


def list_for_user(user_id: str) -> list[dict]:
    if db.enabled():
        with db.pool.connection() as conn:
            return conn.execute(f"select {_LIST_COLS} from posts where user_id=%s order by published_at desc",
                                (user_id,)).fetchall()
    return sorted(({k: p.get(k, 0 if k == "rewrite_count" else None) for k in _LIST_COLS.split(", ")}
                   for p in _posts.values() if p["user_id"] == user_id),
                  key=lambda p: p["published_at"], reverse=True)


def list_all(limit: int = 500) -> list[dict]:
    """Admin content view: every post, every user."""
    if db.enabled():
        with db.pool.connection() as conn:
            return conn.execute(f"select {_LIST_COLS} from posts order by published_at desc limit %s",
                                (limit,)).fetchall()
    return sorted(({k: p.get(k, 0 if k == "rewrite_count" else None) for k in _LIST_COLS.split(", ")}
                   for p in _posts.values()), key=lambda p: p["published_at"], reverse=True)[:limit]


def set_status(post_id: int, status: str, user_id: str | None = None) -> bool:
    """Archive or restore a post. user_id=None is the admin path (any post)."""
    assert status in ("published", "archived")
    if db.enabled():
        sql, params = "update posts set status=%s, updated_at=now() where id=%s", [status, post_id]
        if user_id is not None:
            sql += " and user_id=%s"
            params.append(user_id)
        with db.pool.connection() as conn:
            return conn.execute(sql + " returning id", params).fetchone() is not None
    post = _posts.get(post_id)
    if not post or (user_id is not None and post["user_id"] != user_id):
        return False
    post["status"], post["updated_at"] = status, datetime.now(timezone.utc)
    return True


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
