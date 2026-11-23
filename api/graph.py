"""LangGraph definition and the per-turn runner used by POST /chat.

Current graph (step 4):
    START -> guard -(refuse)-> responder -> END
                   +-(else)-> supervisor -> responder -> END
Later steps add the pipeline nodes (keyword_scout ... optimizer) behind supervisor routes.
"""
import logging
import uuid

from langgraph.graph import END, START, StateGraph

import guard as guard_rules
import run_context
from agents.responder import responder
from agents.supervisor import supervisor
from state import RankForgeState
from stores import db

log = logging.getLogger("rankforge.graph")

_graph = None
_checkpointer = None


# ---------- nodes ----------

def guard(state: RankForgeState) -> dict:
    verdict = guard_rules.check(state.get("user_message", ""))
    if verdict:
        kind, reply = verdict
        log.info("guard refused (%s)", kind)
        return {"route": "refuse", "reply": reply}
    return {"route": ""}


def after_guard(state: RankForgeState) -> str:
    return "responder" if state.get("route") == "refuse" else "supervisor"


# ---------- build ----------

def build(checkpointer):
    g = StateGraph(RankForgeState)
    g.add_node("guard", guard)
    g.add_node("supervisor", supervisor)
    g.add_node("responder", responder)

    g.add_edge(START, "guard")
    g.add_conditional_edges("guard", after_guard, {"responder": "responder", "supervisor": "supervisor"})
    g.add_edge("supervisor", "responder")
    g.add_edge("responder", END)
    return g.compile(checkpointer=checkpointer)


def init() -> None:
    """Open the DB pool, set up the Postgres checkpointer (short-term memory), compile the graph."""
    global _graph, _checkpointer
    pool = db.open_pool()
    if pool is not None:
        from langgraph.checkpoint.postgres import PostgresSaver
        _checkpointer = PostgresSaver(pool)
        _checkpointer.setup()
        log.info("checkpointer: Postgres")
    else:
        from langgraph.checkpoint.memory import InMemorySaver
        _checkpointer = InMemorySaver()
        log.warning("checkpointer: in-memory (lost on restart; set DATABASE_URL for deployment)")
    _graph = build(_checkpointer)


def shutdown() -> None:
    db.close_pool()


# ---------- per-turn runner ----------

def thread_key(user_id: str, thread_id: str) -> str:
    """Checkpoint key is namespaced by user, so two users reusing a thread_id never share history."""
    return f"{user_id}:{thread_id}"


def run_turn(user_id: str, thread_id: str, message: str) -> dict:
    ctx = run_context.RunContext(run_id=uuid.uuid4().hex, user_id=user_id, thread_id=thread_id)
    token = run_context.set_run(ctx)
    try:
        db.ensure_user(user_id)
        config = {"configurable": {"thread_id": thread_key(user_id, thread_id)}}
        turn_input = {
            "user_id": user_id,
            "thread_id": thread_id,
            "user_message": message,
            "messages": [{"role": "user", "content": message}],
            # per-turn fields reset every turn
            "route": "",
            "reply": "",
            "degraded": False,
        }
        out = _graph.invoke(turn_input, config)
        reply = out.get("reply") or "Sorry, I couldn't produce a reply."
    except Exception:
        log.exception("turn failed")
        reply = "Sorry, something went wrong while handling that. Please try again."
    finally:
        run_context.reset_run(token)

    return {
        "reply": reply,
        "tool_calls": ctx.tool_calls,
        "tokens": {"in": ctx.tokens_in, "out": ctx.tokens_out},
    }
