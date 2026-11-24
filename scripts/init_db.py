"""Apply db/schema.sql and db/seed.sql to DATABASE_URL (from api/.env), set demo/admin passwords,
then print row counts.

Usage (from the repo root, with the venv active):  python scripts/init_db.py
Safe to re-run: everything uses IF NOT EXISTS / ON CONFLICT DO NOTHING.

Passwords: demo_alice and demo_bob get DEMO_PASSWORD, admin gets ADMIN_PASSWORD (from .env). If one is
empty, a random password is generated and printed once. Accounts with a password can only be used
through a login session, so nobody can act as them by sending their user_id to the API.
"""
import secrets
import sys
from pathlib import Path

import psycopg
from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parent.parent
# repo-root .env or api/.env; api/.env wins (same order as api/config.py)
env = {**dotenv_values(ROOT / ".env"), **dotenv_values(ROOT / "api" / ".env")}
url = env.get("DATABASE_URL", "")

PLACEHOLDERS = ("YOUR-DATABASE-PASSWORD", "postgres.xxxx", ":password@")
if not url or any(p in url for p in PLACEHOLDERS):
    sys.exit("Set a real DATABASE_URL in api/.env first (copy .env.example to api/.env).")

sys.path.insert(0, str(ROOT / "api"))
from middleware.auth import hash_password  # noqa: E402  (same hash format the app verifies)


def password(var: str, who: str) -> str:
    value = env.get(var) or ""
    if not value:
        value = secrets.token_urlsafe(9)
        print(f"{var} not set: generated a password for {who}: {value}   (save it; it is not shown again)")
    return value


with psycopg.connect(url, autocommit=True, prepare_threshold=None, connect_timeout=15) as conn:
    for name in ("schema.sql", "seed.sql"):
        conn.execute((ROOT / "db" / name).read_text(encoding="utf-8"))
        print(f"applied db/{name}")
    demo = password("DEMO_PASSWORD", "demo_alice and demo_bob")
    for user in ("demo_alice", "demo_bob"):
        conn.execute("update users set password_hash=%s where user_id=%s and password_hash is null",
                     (hash_password(demo), user))
    conn.execute("update users set password_hash=%s, is_admin=true where user_id='admin'"
                 " and password_hash is null", (hash_password(password("ADMIN_PASSWORD", "admin")),))
    print("passwords set for accounts that had none (existing passwords are kept)")
    row = conn.execute(
        "select (select count(*) from users), (select count(*) from affiliate_programs),"
        " (select count(*) from posts), (select count(*) from rankings), (select count(*) from clicks)"
    ).fetchone()
    print("users=%s programs=%s posts=%s rankings=%s clicks=%s" % row)
