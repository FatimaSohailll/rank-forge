"""Observability: wraps every graph node and writes one span per run of it.

Span = node, latency, tokens in/out, cost, tool calls made during the node, and exit status
(ok | refused | halted | invalid | paused | error). Spans go to the `spans` table (admin analytics,
daily cost) and to the log as one JSON line. The wrapper also hosts the two per-node middleware
checks: the cost guard (a CostCeilingHit halts the node cleanly) and the state validator.
"""
import functools
import json
import logging
import time

from langgraph.errors import GraphBubbleUp

import run_context
from middleware import state_validator
from middleware.cost_guard import CostCeilingHit

log = logging.getLogger("rankforge.spans")


def _ctx():
    try:
        return run_context.current()
    except RuntimeError:
        return None


def _emit(span: dict) -> None:
    log.info(json.dumps({"event": "span", **{k: v for k, v in span.items() if k != "tool_calls"},
                         "tools": [c["name"] for c in span["tool_calls"]]}, default=str))
    try:
        from stores import spans
        spans.record(span)
    except Exception:                                # observability must never break a turn
        log.warning("span write failed", exc_info=True)


def instrument(name: str, fn):
    """Wrap a node function (state -> partial update)."""

    @functools.wraps(fn)
    def node(state):
        ctx = _ctx()
        if ctx and ctx.progress:
            ctx.progress(name, "started")
        before = (ctx.tokens_in, ctx.tokens_out, ctx.cost_usd, len(ctx.tool_calls)) if ctx else (0, 0, 0.0, 0)
        started = time.perf_counter()
        status, error, out = "ok", None, {}
        try:
            out = fn(state)
        except GraphBubbleUp:                        # LangGraph interrupt (human approval): not an error
            status = "paused"
            raise
        except CostCeilingHit as e:
            status, error = "halted", str(e)
            if ctx is not None:
                ctx.halted = str(e)
            out = {}
        except Exception as e:
            status, error = "error", f"{type(e).__name__}: {e}"[:500]
            raise
        finally:
            if status in ("paused", "error"):
                if ctx and ctx.progress:
                    ctx.progress(name, status)
                _emit(_span(name, ctx, before, started, status, error))
        out, problems = state_validator.check(name, out)
        if problems:
            status, error = "invalid", "; ".join(problems)[:500]
        elif isinstance(out, dict) and out.get("route") == "refuse":
            status = "refused"
        if ctx and ctx.progress:
            progress_status = "complete" if status == "ok" else status
            expected = {"writer": "drafted", "reviser": "drafted", "content_planner": "planned",
                        "editor": "edited", "publisher": "published"}
            if status == "ok" and name in expected and out.get("stage") != expected[name]:
                progress_status = "incomplete"
            ctx.progress(name, progress_status)
        _emit(_span(name, ctx, before, started, status, error))
        return out

    return node


def _span(name, ctx, before, started, status, error) -> dict:
    tin, tout, cost, ncalls = before
    return {
        "run_id": ctx.run_id if ctx else "no-run",
        "user_id": ctx.user_id if ctx else None,
        "node": name,
        "tokens_in": (ctx.tokens_in - tin) if ctx else 0,
        "tokens_out": (ctx.tokens_out - tout) if ctx else 0,
        "cost_usd": round((ctx.cost_usd - cost) if ctx else 0.0, 6),
        "latency_ms": int((time.perf_counter() - started) * 1000),
        "tool_calls": list(ctx.tool_calls[ncalls:]) if ctx else [],
        "status": status,
        "error": error,
    }
