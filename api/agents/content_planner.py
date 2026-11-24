"""Content Planner: turns the chosen keyword into a structured brief.

Reads the live top results (as untrusted data), one Haiku call writes the brief, and the
output is validated and clamped before it is stored.
"""
import logging

import llm
from state import RankForgeState
from stores import affiliate, briefs
from tools import ToolUnavailable
from tools import web

log = logging.getLogger("rankforge.planner")

SYSTEM_PROMPT = f"""You are the Content Planner for RankForge, an SEO and affiliate content engine.
Write a content brief for an article that can rank for the target keyword.

{web.UNTRUSTED_NOTICE}

Use the competitor results only to see what is already covered and find a better angle.
The article must be helpful and honest, with a clear affiliate angle only where it genuinely fits.
The title and headings must not claim testing or hands-on review, and must not contain prices,
percentages or "best-selling"/"#1" claims, because nobody has verified them.
Choose affiliate programs only from the provided list (or none).

Respond with JSON: title, meta_description (max 155 chars), angle, word_count_target (800-2000),
outline (3-7 items, each with heading and 2-4 short points), faq (2-4 questions), affiliate_programs."""

SCHEMA = {
    "type": "object",
    "properties": {
        "title": {"type": "string"},
        "meta_description": {"type": "string"},
        "angle": {"type": "string"},
        "word_count_target": {"type": "integer"},
        "outline": {"type": "array", "items": {
            "type": "object",
            "properties": {"heading": {"type": "string"}, "points": {"type": "array", "items": {"type": "string"}}},
            "required": ["heading", "points"], "additionalProperties": False}},
        "faq": {"type": "array", "items": {"type": "string"}},
        "affiliate_programs": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["title", "meta_description", "angle", "word_count_target", "outline", "faq",
                 "affiliate_programs"],
    "additionalProperties": False,
}


def _clean(raw: dict, keyword: str, program_names: set[str]) -> dict:
    """Validate and clamp model output so a bad or manipulated response cannot reach the store."""
    outline = []
    for item in (raw.get("outline") or [])[:7]:
        if isinstance(item, dict) and str(item.get("heading", "")).strip():
            points = [str(p)[:200] for p in (item.get("points") or [])[:4]]
            outline.append({"heading": str(item["heading"]).strip()[:120], "points": points})
    try:
        words = int(raw.get("word_count_target", 1200))
    except (TypeError, ValueError):
        words = 1200
    return {
        "keyword": keyword,
        "title": str(raw.get("title") or keyword.title())[:120],
        "meta_description": str(raw.get("meta_description") or "")[:155],
        "angle": str(raw.get("angle") or "")[:400],
        "word_count_target": max(600, min(2500, words)),
        "outline": outline,
        "faq": [str(q)[:160] for q in (raw.get("faq") or [])[:4]],
        "affiliate_programs": [p for p in (raw.get("affiliate_programs") or []) if p in program_names][:3],
    }


def _format_reply(brief: dict, degraded: bool) -> str:
    lines = [f"Brief ready for **{brief['keyword']}**", f"Title: {brief['title']}",
             f"Angle: {brief['angle']}", f"Target length: ~{brief['word_count_target']} words", "Outline:"]
    lines += [f"- {s['heading']}" for s in brief["outline"]]
    if brief["affiliate_programs"]:
        lines.append("Affiliate programs: " + ", ".join(brief["affiliate_programs"]))
    if degraded:
        lines.append("(Note: live competitor data was unavailable, so this brief is based on the keyword alone.)")
    return "\n".join(lines)


def content_planner(state: RankForgeState) -> dict:
    keyword = (state.get("keyword") or "").strip()
    if not keyword:
        return {"reply": "Which keyword or topic should I plan an article for?"}

    degraded = False
    serp_block = "No competitor data available."
    try:
        results = web.web_search(keyword, max_results=5)
        serp_block = "\n\n".join(f"{r['title']}\n{r['snippet']}" for r in results) or serp_block
    except ToolUnavailable:
        degraded = True

    programs = affiliate.list_programs()
    program_names = {p["name"] for p in programs}
    meta = state.get("keyword_meta") or {}
    profile = state.get("profile") or {}
    profile_text = "\n".join(f"- {k}: {v}" for k, v in profile.items())
    user_msg = (
        (f"Site profile (match audience, tone and niche):\n{profile_text}\n\n" if profile_text else "")
        + f"Target keyword: {keyword}\n"
        f"Estimated signals: demand {meta.get('demand', 'n/a')}, intent {meta.get('intent', 'n/a')}, "
        f"competition {meta.get('competition', 'n/a')}\n"
        f"Available affiliate programs: {', '.join(sorted(program_names)) or 'none'}\n\n"
        f"Top competitor results:\n{serp_block}"
    )
    try:
        raw = llm.complete(SYSTEM_PROMPT, [{"role": "user", "content": user_msg}], max_tokens=900, schema=SCHEMA)
    except llm.LLMRefusal:
        return {"reply": "I couldn't plan an article for that topic."}
    brief = _clean(raw if isinstance(raw, dict) else {}, keyword, program_names)
    brief["id"] = briefs.save_brief(state["user_id"], keyword, brief)
    return {"brief": brief, "reply": _format_reply(brief, degraded), "degraded": degraded, "stage": "planned"}
