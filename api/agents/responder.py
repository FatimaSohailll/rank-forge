"""Responder: finalises the turn and appends the assistant message to short-term memory.

For now it passes the supervisor's reply through (no extra LLM call). Later steps make
it summarise what the pipeline nodes produced.
"""
from state import RankForgeState

FALLBACK = "Sorry, I couldn't put together an answer this time. Could you rephrase that?"


def responder(state: RankForgeState) -> dict:
    reply = (state.get("reply") or "").strip() or FALLBACK
    return {"reply": reply, "messages": [{"role": "assistant", "content": reply}]}
