"""Writer: generates the full article (Markdown) from the brief."""
import llm
from state import RankForgeState
from stores import drafts

SYSTEM_PROMPT = """You are the Writer for RankForge, an SEO and affiliate content engine.
Write a complete, helpful article in Markdown from the brief.

Rules:
- Exactly one H1 (the title, containing the target keyword). Use the outline headings as H2 sections.
- Mention the target keyword naturally in the first 100 words and in at least one H2. Do not stuff it.
- Plain language: short sentences, short paragraphs (under 90 words), no filler or hype.
- Do NOT invent statistics, prices, specifications, studies or quotes. Where you lack a hard fact,
  describe the idea in general terms instead of making up a number.
- Never say or imply that you tested, reviewed hands-on or personally ranked any product. Titles and
  headings must not contain prices, percentages, "tested", "best-selling" or "#1" claims.
- You may name product types and well-known products, because the article will link to retailers.
  Recommend honestly, with trade-offs.
- End with an FAQ section (H2) answering the brief's questions briefly.
- Output only the article Markdown, nothing before or after it."""


def _brief_text(brief: dict, keyword: str) -> str:
    outline = "\n".join(f"- {s['heading']}: " + "; ".join(s.get("points", [])) for s in brief.get("outline", []))
    return (f"Target keyword: {keyword}\nTitle: {brief.get('title')}\nAngle: {brief.get('angle')}\n"
            f"Target length: about {brief.get('word_count_target', 1200)} words\n"
            f"Outline:\n{outline}\nFAQ questions: {'; '.join(brief.get('faq', []))}\n"
            f"Affiliate programs to fit in naturally: {', '.join(brief.get('affiliate_programs', [])) or 'none'}")


def writer(state: RankForgeState) -> dict:
    brief = state.get("brief") or {}
    keyword = brief.get("keyword") or state.get("keyword") or ""
    if not brief or not keyword:
        return {"reply": "I need a brief before I can write the article."}
    profile = state.get("profile") or {}
    system = SYSTEM_PROMPT
    if profile:
        system += "\n\nSite profile (follow it): " + "; ".join(f"{k}: {v}" for k, v in profile.items())
    target = int(brief.get("word_count_target", 1200))
    try:
        text = llm.complete(system, [{"role": "user", "content": _brief_text(brief, keyword)}],
                            max_tokens=min(4500, target * 3))
    except llm.LLMRefusal:
        return {"reply": "I couldn't write an article on that topic."}
    draft = text.strip() if isinstance(text, str) else ""
    if not draft:
        return {"reply": "I wasn't able to produce a draft this time. Please try again."}
    draft_id = drafts.save_draft(state["user_id"], brief.get("id"), "draft", draft)
    return {"draft": draft, "draft_id": draft_id, "stage": "drafted"}
