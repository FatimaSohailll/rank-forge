"""Apply db/schema.sql and db/seed.sql to DATABASE_URL (from api/.env), then print row counts.

Usage (from the repo root, with the venv active):  python scripts/init_db.py
Safe to re-run: everything uses IF NOT EXISTS / ON CONFLICT DO NOTHING.
"""
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

with psycopg.connect(url, autocommit=True, prepare_threshold=None, connect_timeout=15) as conn:
    for name in ("schema.sql", "seed.sql"):
        conn.execute((ROOT / "db" / name).read_text(encoding="utf-8"))
        print(f"applied db/{name}")
    row = conn.execute(
        "select (select count(*) from users), (select count(*) from affiliate_programs),"
        " (select count(*) from posts), (select count(*) from rankings), (select count(*) from clicks)"
    ).fetchone()
    print("users=%s programs=%s posts=%s rankings=%s clicks=%s" % row)
