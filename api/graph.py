"""LangGraph definition and the per-turn runner used by POST /chat.

Current graph:
    START -> guard -(refuse)-> responder -> END
                   +-(else)-> memory -> supervisor -+-(remember)-> remember -> responder
                                          +-(keyword_research)-> keyword_scout -+-> responder -> END
                                          +-(write_article)---> keyword_scout -> content_planner -> writer
                                          +-(publish)---------> publish_gate -(approved)-> publisher -> responder
                                          +-(other)-----------> responder
    writer -+-> fact_checker -+-> editor -> responder   (QA runs in parallel, editor joins)
            +-> link_builder -+
publish_gate pauses the graph (LangGraph interrupt) until a human approves or rejects; see "approvals" below.
Later increments add trackers and optimizer.
"""
import logging
import queue
import re
import threading
import time
import uuid

from langgraph.graph import END, START, StateGraph
from langgraph.types import Command

import guard as guard_rules
import run_context
from agents.content_planner import content_planner
from agents.editor import editor
from agents.fact_checker import fact_checker
from agents.keyword_scout import keyword_scout
from agents.link_builder import link_builder
from agents.memory import memory_node
from agents.publish import after_gate, publish_gate, publisher
from agents.remember import remember
from agents.responder import responder
from agents.supervisor import supervisor
from agents.writer import writer
from state import RankForgeState
from stores import approvals, db

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
    return "responder" if state.get("route") == "refuse" else "memory"


def after_supervisor(state: RankForgeState) -> str:
    route = state.get("route")
    if route == "remember":
        return "remember"
    if route == "publish":
        return "publish_gate"
    if route == "keyword_research":
        return "keyword_scout"
    if route == "write_article":
        if state.get("topic"):
            return "keyword_scout"                      # scout picks/scores, planner follows
        return "content_planner" if state.get("keyword") else "responder"
    return "responder"


def after_scout(state: RankForgeState) -> str:
    return "content_planner" if state.get("route") == "write_article" and state.get("keyword") else "responder"


def after_planner(state: RankForgeState) -> str:
    return "writer" if state.get("stage") == "planned" else "responder"


def after_writer(state: RankForgeState) -> list[str]:
    """Fan out to the two QA nodes in parallel; skip them if no draft was produced."""
    return ["fact_checker", "link_builder"] if state.get("stage") == "drafted" else ["responder"]


# ---------- build ----------

def build(checkpointer):
    g = StateGraph(RankForgeState)
    g.add_node("guard", guard)
    g.add_node("memory", memory_node)
    g.add_node("supervisor", supervisor)
    g.add_node("remember", remember)
    g.add_node("keyword_scout", keyword_scout)
    g.add_node("content_planner", content_planner)
    g.add_node("writer", writer)
    g.add_node("fact_checker", fact_checker)
    g.add_node("link_builder", link_builder)
    g.add_node("editor", editor)
    g.add_node("publish_gate", publish_gate)
    g.add_node("publisher", publisher)
    g.add_node("responder", responder)

    g.add_edge(START, "guard")
    g.add_conditional_edges("guard", after_guard, {"responder": "responder", "memory": "memory"})
    g.add_edge("memory", "supervisor")
    g.add_edge("remember", "responder")
    g.add_conditional_edges("supervisor", after_supervisor, {
        "remember": "remember", "publish_gate": "publish_gate", "keyword_scout": "keyword_scout",
        "content_planner": "content_planner", "responder": "responder"})
    g.add_conditional_edges("publish_gate", after_gate, {"publisher": "publisher", "responder": "responder"})
    g.add_edge("publisher", "responder")
    g.add_conditional_edges("keyword_scout", after_scout, {
        "content_planner": "content_planner", "responder": "responder"})
    g.add_conditional_edges("content_planner", after_planner, {"writer": "writer", "responder": "responder"})
    g.add_conditional_edges("writer", after_writer, ["fact_checker", "link_builder", "responder"])
    g.add_edge(["fact_checker", "link_builder"], "editor")      # join: editor waits for both
    g.add_edge("editor", "responder")
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


def _turn_input(user_id: str, thread_id: str, message: str) -> dict:
    return {
        "user_id": user_id,
        "thread_id": thread_id,
        "user_message": message,
        "messages": [{"role": "user", "content": message}],
        # per-turn fields reset every turn
        "route": "",
        "reply": "",
        "topic": "",
        "location": "",
        "stage": "",
        "approval": {},
        "flagged_claims": [],
        "fact_check_notes": [],
        "link_notes": [],
        "degraded": False,
    }


def _result(reply: str, ctx: run_context.RunContext) -> dict:
    return {
        "reply": reply,
        "tool_calls": ctx.tool_calls,
        "tokens": {"in": ctx.tokens_in, "out": ctx.tokens_out},
    }


# Shown to the user as each node finishes (a full article takes ~45s, so progress matters).
NODE_LABELS = {
    "guard": "Checked your request",
    "memory": "Loaded your profile",
    "remember": "Updated your saved profile",
    "supervisor": "Understood your request",
    "keyword_scout": "Researched keywords",
    "content_planner": "Planned the article",
    "writer": "Wrote the draft",
    "fact_checker": "Fact-checked the claims",
    "link_builder": "Added affiliate links",
    "editor": "Edited and scored the article",
    "publish_gate": "Got your approval",
    "publisher": "Published the article",
}
KEEPALIVE_S = 10.0


# ---------- approvals (human in the loop) ----------
# A pause is stored in the `approvals` table with a random resume token. It is resolved exactly once,
# only by its owner, either from chat ("approve" / "reject") or via POST /resume. Chat decisions are
# matched deterministically (no LLM), so a manipulated model can never approve on a user's behalf.

APPROVE = re.compile(r"^\s*(approve|approved|yes|y|confirm|confirmed|go ahead|ok|okay)\s*[.!]*\s*$", re.IGNORECASE)
REJECT = re.compile(r"^\s*(reject|rejected|no|n|cancel|stop|don'?t publish|do not publish|never ?mind)\s*[.!]*\s*$",
                    re.IGNORECASE)
# Unambiguous decision words. With nothing pending they get a fixed answer instead of a model-written one
# (otherwise the model might say "Approved." about nothing); "yes"/"ok"/"no" stay ordinary chat.
DECISION_WORD = re.compile(r"^\s*(approve|approved|reject|rejected)\s*[.!]*\s*$", re.IGNORECASE)
NOTHING_PENDING = "There's nothing waiting for your approval right now."
STATUS = {"approve": "approved", "reject": "rejected", "amend": "amended"}


def parse_decision(message: str) -> str | None:
    if APPROVE.match(message):
        return "approve"
    if REJECT.match(message):
        return "reject"
    return None


def _approval_prompt(payload: dict) -> str:
    bits = [f"{payload.get('words', '?')} words"]
    if payload.get("seo_score") is not None:
        bits.append(f"SEO {payload['seo_score']}/100")
    bits.append(f"{payload.get('affiliate_links', 0)} tracked affiliate link(s)")
    return (f"I'm ready to publish **{payload.get('title', 'your article')}** ({', '.join(bits)}). "
            "Nothing is public yet. Reply **approve** to publish it or **reject** to cancel.")


def _approval_reminder(pending: dict) -> str:
    title = (pending.get("action") or {}).get("title", "your article")
    return (f"You have a publish request waiting for **{title}**. Reply **approve** to publish it or "
            "**reject** to cancel before we continue.")


def _open_approval(user_id: str, key: str, payload: dict) -> str:
    approvals.create(user_id, key, "risky_action", payload)
    return _approval_prompt(payload)


def _execute(user_id: str, key: str, graph_input, on_progress=None) -> str:
    """Run (or resume) the graph on one thread and return the reply to show the user."""
    reply, interrupts = "", None
    for chunk in _graph.stream(graph_input, {"configurable": {"thread_id": key}}, stream_mode="updates"):
        for node, update in chunk.items():
            if node == "__interrupt__":
                interrupts = update
                continue
            if on_progress and node in NODE_LABELS:
                on_progress(node)
            if node == "responder" and isinstance(update, dict):
                reply = update.get("reply") or reply
    if interrupts:                                   # the graph paused for a human decision
        return _open_approval(user_id, key, interrupts[0].value)
    return reply or "Sorry, I couldn't produce a reply."


def _resume(user_id: str, token: str, decision: str, amend: dict | None, on_progress=None) -> str:
    resolved = approvals.resolve(user_id, token, STATUS[decision])   # exactly once, owner only
    if not resolved:
        return "I couldn't find a pending approval for that request. It may already have been handled."
    payload = {"decision": decision, "draft_id": (resolved["action"] or {}).get("draft_id"), "amend": amend or {}}
    return _execute(user_id, resolved["thread_key"], Command(resume=payload), on_progress)


def _handle(user_id: str, thread_id: str, message: str, on_progress=None) -> str:
    db.ensure_user(user_id)
    key = thread_key(user_id, thread_id)
    pending = approvals.pending_for_thread(user_id, key)
    if pending:
        decision = parse_decision(message)
        if decision is None:
            return _approval_reminder(pending)
        return _resume(user_id, pending["resume_token"], decision, None, on_progress)
    if DECISION_WORD.match(message):
        return NOTHING_PENDING
    return _execute(user_id, key, _turn_input(user_id, thread_id, message), on_progress)


# ---------- entry points ----------

def run_turn(user_id: str, thread_id: str, message: str) -> dict:
    ctx = run_context.RunContext(run_id=uuid.uuid4().hex, user_id=user_id, thread_id=thread_id)
    token = run_context.set_run(ctx)
    try:
        reply = _handle(user_id, thread_id, message)
    except Exception:
        log.exception("turn failed")
        reply = "Sorry, something went wrong while handling that. Please try again."
    finally:
        run_context.reset_run(token)
    return _result(reply, ctx)


def resume_turn(user_id: str, resume_token: str, decision: str, amend: dict | None = None) -> dict:
    """POST /resume: resolve a pending approval by token (approve | reject | amend)."""
    ctx = run_context.RunContext(run_id=uuid.uuid4().hex, user_id=user_id, thread_id="resume")
    token = run_context.set_run(ctx)
    try:
        db.ensure_user(user_id)
        reply = _resume(user_id, resume_token, decision, amend)
    except Exception:
        log.exception("resume failed")
        reply = "Sorry, something went wrong while handling that. Please try again."
    finally:
        run_context.reset_run(token)
    return _result(reply, ctx)


def stream_turn(user_id: str, thread_id: str, message: str):
    """Run a turn in a worker thread and yield (event, data): progress..., then done.

    The worker owns the RunContext (a ContextVar set inside a generator would be lost between
    iterations), and a keepalive is yielded while a long step is running.
    """
    ctx = run_context.RunContext(run_id=uuid.uuid4().hex, user_id=user_id, thread_id=thread_id)
    events: queue.Queue = queue.Queue()
    started = time.monotonic()

    def progress(node: str) -> None:
        events.put(("progress", {"node": node, "label": NODE_LABELS[node],
                                 "seconds": round(time.monotonic() - started, 1)}))

    def worker() -> None:
        token = run_context.set_run(ctx)
        try:
            reply = _handle(user_id, thread_id, message, progress)
        except Exception:
            log.exception("streamed turn failed")
            reply = "Sorry, something went wrong while handling that. Please try again."
        finally:
            run_context.reset_run(token)
        events.put(("done", _result(reply, ctx)))

    threading.Thread(target=worker, daemon=True, name="rankforge-turn").start()
    while True:
        try:
            event, data = events.get(timeout=KEEPALIVE_S)
        except queue.Empty:
            yield "keepalive", {}
            continue
        yield event, data
        if event == "done":
            return
