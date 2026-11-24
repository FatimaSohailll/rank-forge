"""Observability spans (table `spans`): one row per graph node run.

Written by middleware/observability.py; read by the admin analytics page and the cost guard
(today's spend survives restarts because it is summed from here). Postgres when available,
a bounded in-process buffer otherwise.
"""
from collections import deque
from datetime import datetime, timezone

from psycopg.types.json import Jsonb

from stores import db

_mem: deque = deque(maxlen=5000)


def record(span: dict) -> None:
    span = {**span, "created_at": datetime.now(timezone.utc)}
    if db.enabled():
        with db.pool.connection() as conn:
            conn.execute(
                "insert into spans (run_id, user_id, node, tokens_in, tokens_out, cost_usd, latency_ms,"
                " tool_calls, status, error) values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (span["run_id"], span.get("user_id"), span["node"], span.get("tokens_in", 0),
                 span.get("tokens_out", 0), span.get("cost_usd", 0.0), span.get("latency_ms"),
                 Jsonb(span.get("tool_calls") or []), span.get("status", "ok"), span.get("error")))
        return
    _mem.append(span)


def recent(limit: int = 100) -> list[dict]:
    if db.enabled():
        with db.pool.connection() as conn:
            return conn.execute(
                "select run_id, user_id, node, tokens_in, tokens_out, cost_usd, latency_ms, tool_calls, status,"
                " error, created_at from spans order by id desc limit %s", (limit,)).fetchall()
    return list(reversed(_mem))[:limit]


def spend_today() -> float:
    """USD spent on LLM calls today (UTC), across all users."""
    if db.enabled():
        with db.pool.connection() as conn:
            row = conn.execute("select coalesce(sum(cost_usd),0) as c from spans"
                               " where created_at >= date_trunc('day', now() at time zone 'utc')"
                               " at time zone 'utc'").fetchone()
        return float(row["c"])
    today = datetime.now(timezone.utc).date()
    return float(sum(s.get("cost_usd", 0.0) for s in _mem if s["created_at"].date() == today))


def summary(days: int = 7) -> dict:
    """Admin analytics: totals and a per-node breakdown for the last `days` days."""
    if db.enabled():
        with db.pool.connection() as conn:
            totals = conn.execute(
                "select count(distinct run_id) as runs, count(*) as spans, coalesce(sum(tokens_in),0) as tokens_in,"
                " coalesce(sum(tokens_out),0) as tokens_out, coalesce(sum(cost_usd),0) as cost_usd,"
                " count(*) filter (where status='error') as errors,"
                " count(*) filter (where status='refused') as refused,"
                " count(*) filter (where status='halted') as halted,"
                " count(distinct user_id) as users"
                " from spans where created_at > now() - make_interval(days => %s)", (days,)).fetchone()
            nodes = conn.execute(
                "select node, count(*) as runs, round(avg(latency_ms)) as avg_ms, max(latency_ms) as max_ms,"
                " coalesce(sum(cost_usd),0) as cost_usd, count(*) filter (where status<>'ok') as not_ok"
                " from spans where created_at > now() - make_interval(days => %s)"
                " group by node order by cost_usd desc, runs desc", (days,)).fetchall()
            daily = conn.execute(
                "select date_trunc('day', created_at)::date as day, count(distinct run_id) as runs,"
                " coalesce(sum(cost_usd),0) as cost_usd from spans"
                " where created_at > now() - make_interval(days => %s) group by 1 order by 1", (days,)).fetchall()
        return {"totals": {k: (float(v) if k == "cost_usd" else v) for k, v in totals.items()},
                "nodes": [{**n, "cost_usd": float(n["cost_usd"])} for n in nodes],
                "daily": [{**d, "cost_usd": float(d["cost_usd"])} for d in daily]}
    rows = list(_mem)
    by_node: dict[str, dict] = {}
    for s in rows:
        n = by_node.setdefault(s["node"], {"node": s["node"], "runs": 0, "lat": [], "cost_usd": 0.0, "not_ok": 0})
        n["runs"] += 1
        n["lat"].append(s.get("latency_ms") or 0)
        n["cost_usd"] += s.get("cost_usd", 0.0)
        n["not_ok"] += s.get("status", "ok") != "ok"
    nodes = [{"node": n["node"], "runs": n["runs"], "avg_ms": round(sum(n["lat"]) / len(n["lat"])),
              "max_ms": max(n["lat"]), "cost_usd": n["cost_usd"], "not_ok": n["not_ok"]} for n in by_node.values()]
    totals = {"runs": len({s["run_id"] for s in rows}), "spans": len(rows),
              "tokens_in": sum(s.get("tokens_in", 0) for s in rows),
              "tokens_out": sum(s.get("tokens_out", 0) for s in rows),
              "cost_usd": sum(s.get("cost_usd", 0.0) for s in rows),
              "errors": sum(s.get("status") == "error" for s in rows),
              "refused": sum(s.get("status") == "refused" for s in rows),
              "halted": sum(s.get("status") == "halted" for s in rows),
              "users": len({s.get("user_id") for s in rows})}
    return {"totals": totals, "nodes": sorted(nodes, key=lambda n: (-n["cost_usd"], -n["runs"])), "daily": []}


def clear_memory() -> None:
    """Test helper for the in-process fallback."""
    _mem.clear()
