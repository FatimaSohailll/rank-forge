"""LangGraph definition and the per-turn runner used by POST /chat.

Graph:
    START -> guard -(refuse)-> responder -> END
                   +-(else)-> memory -> supervisor -+-(remember)-> remember -> responder
                                          +-(keyword_research)-> keyword_scout -+-> responder -> END
                                          +-(write_article)---> keyword_scout -> content_planner -> writer
                                          +-(publish)---------> publish_gate -(approved)-> publisher -> responder
                                          +-(performance | optimize)-> rank_tracker -+-> optimizer
                                          |                            click_tracker -+
                                          +-(other)-----------> responder
    writer -+-> fact_checker -+-> editor -> responder   (QA runs in parallel, editor joins)
            +-> link_builder -+
    optimizer -(optimize + rewrite)--> content_planner -> writer -> ...   (re-write loop; publish replaces the post)
              -(optimize + retarget)-> keyword_scout -> responder          (re-target loop)
              -(report / hold)-------> responder
publish_gate pauses the graph (LangGraph interrupt) until a human approves or rejects; see "approvals" below.
Every node is wrapped by middleware/observability.instrument (spans, cost-guard halt, state validation).
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
from agents.click_tracker import click_tracker
from agents.content_planner import content_planner
from agents.editor import editor
from agents.fact_checker import fact_checker
from agents.keyword_scout import keyword_scout
from agents.link_builder import link_builder
from agents.memory import memory_node
from agents.optimizer import after_optimizer, optimizer
from agents.publish import after_gate, publish_gate, publisher
from agents.rank_tracker import rank_tracker
from agents.remember import remember
from agents.responder import responder
from agents.supervisor import supervisor
from agents.writer import writer
from agents.reviser import reviser
from middleware.observability import instrument
from state import RankForgeState
from stores import approvals, db, users

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


def after_supervisor(state: RankForgeState) -> str | list[str]:
    route = state.get("route")
    if route in ("performance", "optimize"):
        return ["rank_tracker", "click_tracker"]       # Monitor lane runs in parallel; optimizer joins
    if route == "revise":
        return "reviser"
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

NODES = {
    "reviser": reviser, "guard": guard, "memory": memory_node, "supervisor": supervisor, "remember": remember,
    "keyword_scout": keyword_scout, "content_planner": content_planner, "writer": writer,
    "fact_checker": fact_checker, "link_builder": link_builder, "editor": editor,
    "publish_gate": publish_gate, "publisher": publisher,
    "rank_tracker": rank_tracker, "click_tracker": click_tracker, "optimizer": optimizer,
    "responder": responder,
}


def build(checkpointer):
    g = StateGraph(RankForgeState)
    for name, fn in NODES.items():
        g.add_node(name, instrument(name, fn))           # observability + cost guard + state validator

    g.add_edge(START, "guard")
    g.add_conditional_edges("guard", after_guard, {"responder": "responder", "memory": "memory"})
    g.add_edge("memory", "supervisor")
    g.add_edge("remember", "responder")
    g.add_conditional_edges("supervisor", after_supervisor, [
        "reviser", "remember", "publish_gate", "keyword_scout", "content_planner", "rank_tracker", "click_tracker",
        "responder"])
    g.add_edge(["rank_tracker", "click_tracker"], "optimizer")   # join: optimizer waits for both
    g.add_conditional_edges("optimizer", after_optimizer, {
        "content_planner": "content_planner", "keyword_scout": "keyword_scout", "responder": "responder"})
    g.add_conditional_edges("publish_gate", after_gate, {"publisher": "publisher", "responder": "responder"})
    g.add_edge("publisher", "responder")
    g.add_conditional_edges("keyword_scout", after_scout, {
        "content_planner": "content_planner", "responder": "responder"})
    g.add_conditional_edges("content_planner", after_planner, {"writer": "writer", "responder": "responder"})
    g.add_conditional_edges("writer", after_writer, ["fact_checker", "link_builder", "responder"])
    g.add_conditional_edges("reviser", after_writer, ["fact_checker", "link_builder", "responder"])
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


def _turn_input(user_id: str, thread_id: str, message: str, action: dict | None = None) -> dict:
    action = action or {}
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
        "forced_route": action.get("type", ""),
        "revision_draft_id": int(action.get("draft_id") or 0) if action.get("type") == "revise" else 0,
        "publish_draft_id": int(action.get("draft_id") or 0),
        "target_post_id": int(action.get("post_id") or 0),
        "rewrite_of": 0,
        "optimizer_decision": "",
        "optimizer_notes": "",
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
    "reviser": "Revised your article",
    "fact_checker": "Fact-checked the claims",
    "link_builder": "Added affiliate links",
    "editor": "Edited and scored the article",
    "publish_gate": "Ready for your approval",
    "publisher": "Published the article",
    "rank_tracker": "Read your ranking history",
    "click_tracker": "Read your traffic and clicks",
    "optimizer": "Decided what to optimise",
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
    if payload.get("flagged_claims"):
        bits.append(f"{payload['flagged_claims']} claim(s) were flagged by the fact check and softened")
    if payload.get("replaces_post_id"):
        return (f"I'm ready to replace your live article **{payload.get('replaces_title')}** with the rewrite "
                f"**{payload.get('title', 'your article')}** ({', '.join(bits)}). The live version is unchanged "
                "until you decide. Reply **approve** to replace it or **reject** to cancel.")
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


ACTIONS = {"publish", "optimize", "revise"}


def _handle(user_id: str, thread_id: str, message: str, on_progress=None, action: dict | None = None) -> str:
    users.ensure(user_id)
    key = thread_key(user_id, thread_id)
    pending = approvals.pending_for_thread(user_id, key)
    if pending:
        decision = parse_decision(message)
        if decision is None:
            return _approval_reminder(pending)
        return _resume(user_id, pending["resume_token"], decision, None, on_progress)
    if DECISION_WORD.match(message):
        return NOTHING_PENDING
    if action and action.get("type") not in ACTIONS:
        action = None
    return _execute(user_id, key, _turn_input(user_id, thread_id, message, action), on_progress)


# ---------- entry points ----------

def run_turn(user_id: str, thread_id: str, message: str, action: dict | None = None) -> dict:
    """One chat turn. UI actions select a draft to publish/revise, or a post to optimize."""
    ctx = run_context.RunContext(run_id=uuid.uuid4().hex, user_id=user_id, thread_id=thread_id)
    token = run_context.set_run(ctx)
    try:
        reply = _handle(user_id, thread_id, message, action=action)
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
        users.ensure(user_id)
        reply = _resume(user_id, resume_token, decision, amend)
    except Exception:
        log.exception("resume failed")
        reply = "Sorry, something went wrong while handling that. Please try again."
    finally:
        run_context.reset_run(token)
    return _result(reply, ctx)


def stream_turn(user_id: str, thread_id: str, message: str, action: dict | None = None):
    """Run a turn in a worker thread and yield (event, data): progress..., then done.

    The worker owns the RunContext (a ContextVar set inside a generator would be lost between
    iterations), and a keepalive is yielded while a long step is running.
    """
    ctx = run_context.RunContext(run_id=uuid.uuid4().hex, user_id=user_id, thread_id=thread_id)
    events: queue.Queue = queue.Queue()
    started = time.monotonic()

    def progress(node: str, status: str = "complete") -> None:
        if node in NODE_LABELS:
            events.put(("progress", {"node": node, "label": NODE_LABELS[node], "status": status,
                                     "seconds": round(time.monotonic() - started, 1)}))

    ctx.progress = progress

    def worker() -> None:
        token = run_context.set_run(ctx)
        failed = False
        try:
            reply = _handle(user_id, thread_id, message, action=action)
        except Exception:
            failed = True
            log.exception("streamed turn failed")
            reply = "Sorry, something went wrong while handling that. Please try again."
        finally:
            run_context.reset_run(token)
        result = _result(reply, ctx)
        if ctx.article and not ctx.halted and not failed:
            result["article"] = ctx.article
        result["halted"] = bool(ctx.halted)
        result["error"] = failed
        events.put(("done", result))

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
