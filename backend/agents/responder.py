"""Responder: finalises the turn and appends the assistant message to short-term memory.

For now it passes the supervisor's reply through (no extra LLM call). Later steps make
it summarise what the pipeline nodes produced.
"""
import re

import run_context
from middleware.cost_guard import HALTED_REPLY
from state import RankForgeState
from stores import drafts

ARTICLE_DIVIDER = "\n\n---\n\n"
FALLBACK = "Sorry, I couldn't put together an answer this time. Could you rephrase that?"


def responder(state: RankForgeState) -> dict:
    reply = (state.get("reply") or "").strip() or FALLBACK
    try:
        halted = run_context.current().halted
    except RuntimeError:
        halted = ""
    if halted:
        reply = HALTED_REPLY.format(reason=halted)
    if state.get("stage") == "edited" and not halted and state.get("user_id") and state.get("draft_id"):
        draft = drafts.get_draft(state["user_id"], state.get("draft_id", 0))
        if draft and draft["stage"] == "final":
            title = re.search(r"^#\s+(.+)$", draft["body"], re.MULTILINE)
            report = draft.get("seo_report") or {}
            try:
                run_context.current().article = {
                    "draft_id": draft["id"], "title": title.group(1) if title else "Your article",
                    "words": report.get("stats", {}).get("words", 0), "seo_score": report.get("score"),
                    "workspace_url": f"/app/articles/{draft['id']}"}
            except RuntimeError:
                pass
    remembered = reply
    if state.get("stage") == "edited" and ARTICLE_DIVIDER in reply:
        # The full article is saved in the drafts store; keep only the summary in conversation history.
        remembered = reply.split(ARTICLE_DIVIDER, 1)[0] + "\n(The full article was shown to the user.)"
    return {"reply": reply, "messages": [{"role": "assistant", "content": remembered}]}
