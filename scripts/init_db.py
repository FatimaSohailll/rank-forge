"""Apply db/schema.sql and db/seed.sql to DATABASE_URL (from api/.env), then print row counts.

Usage (from the repo root):  .venv\\Scripts\\python scripts\\init_db.py
Safe to re-run: everything uses IF NOT EXISTS / ON CONFLICT DO NOTHING.
"""
import sys
from pathlib import Path

import psycopg
from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parent.parent
env = dotenv_values(ROOT / "api" / ".env")
url = env.get("DATABASE_URL", "")

if not url or "YOUR-DATABASE-PASSWORD" in url:
    sys.exit("Set DATABASE_URL in api/.env first (replace YOUR-DATABASE-PASSWORD).")

with psycopg.connect(url, autocommit=True, prepare_threshold=None, connect_timeout=15) as conn:
    for name in ("schema.sql", "seed.sql"):
        conn.execute((ROOT / "db" / name).read_text(encoding="utf-8"))
        print(f"applied db/{name}")
    row = conn.execute(
        "select (select count(*) from users), (select count(*) from affiliate_programs),"
        " (select count(*) from posts), (select count(*) from rankings), (select count(*) from clicks)"
    ).fetchone()
    print("users=%s programs=%s posts=%s rankings=%s clicks=%s" % row)
