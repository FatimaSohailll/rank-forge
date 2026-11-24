"""Affiliate program catalog (shared, not user-owned).

With a database it reads the `affiliate_programs` table. Without one (local dev, tests) it
reads the same rows from db/seed.sql, so there is a single source of truth.
"""
import re
from pathlib import Path

from stores import db

SEED = Path(__file__).resolve().parent.parent.parent / "db" / "seed.sql"
_ROW = re.compile(r"\(\s*'([^']+)'\s*,\s*'([^']+)'\s*,\s*'([^']+)'\s*,\s*([\d.]+)\s*,\s*'([^']+)'\s*\)")


def _from_seed() -> list[dict]:
    if not SEED.exists():
        return []
    text = SEED.read_text(encoding="utf-8")
    start = text.find("insert into affiliate_programs")
    if start < 0:
        return []
    block = text[start:text.find(";", start)]
    rows = [{"id": None, "name": m[0], "network": m[1], "category": m[2], "commission_pct": float(m[3]),
             "base_url": m[4]} for m in _ROW.findall(block)]
    return sorted(rows, key=lambda r: r["commission_pct"], reverse=True)


def list_programs() -> list[dict]:
    """[{id, name, network, category, commission_pct, base_url}] ordered by commission."""
    if not db.enabled():
        return _from_seed()
    with db.pool.connection() as conn:
        return conn.execute(
            "select id, name, network, category, commission_pct, base_url from affiliate_programs"
            " order by commission_pct desc"
        ).fetchall()
