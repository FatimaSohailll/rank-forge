"""Postgres connection pool (Supabase pooler friendly).

Rule: every query on a user-owned table takes user_id as a parameter.
"""
import logging

from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from config import settings

log = logging.getLogger("rankforge.db")

pool: ConnectionPool | None = None


def open_pool() -> ConnectionPool | None:
    global pool
    if not settings.database_url:
        log.warning("DATABASE_URL not set: running WITHOUT Postgres (local dev only)")
        return None
    pool = ConnectionPool(
        conninfo=settings.database_url,
        min_size=1,
        max_size=5,
        # autocommit + dict_row are required by LangGraph's PostgresSaver;
        # prepare_threshold=None disables prepared statements (Supabase transaction pooler).
        kwargs={"autocommit": True, "prepare_threshold": None, "row_factory": dict_row},
        open=True,
    )
    return pool


def close_pool() -> None:
    if pool is not None:
        pool.close()


def enabled() -> bool:
    return pool is not None


def ensure_user(user_id: str) -> None:
    """Create the user row on first contact so FK-linked tables work."""
    if pool is None:
        return
    with pool.connection() as conn:
        conn.execute(
            "insert into users (user_id) values (%s) on conflict (user_id) do nothing",
            (user_id,),
        )
