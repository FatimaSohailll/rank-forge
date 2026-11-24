"""Editor: merges the QA outputs into one publish-ready article, audits it, improves it once.

Order: (1) revise flagged claims, (2) SEO audit, (3) at most one improve pass if the score is low.
Model edits are accepted only if all affiliate links survive and the H1 is still there;
otherwise the previous version is kept.
"""
import logging
import re

import llm
from state import RankForgeState
from agents.fact_checker import HEADLINE_CLAIM
from stores import drafts
from tools import seo_audit

log = logging.getLogger("rankforge.editor")

SEO_TARGET = 80
_MD_LINK = re.compile(r"\[[^\]]+\]\([^)]+\)")
_H1 = re.compile(r"^#\s+\S", re.MULTILINE)

REVISE_SYSTEM = """You are the Editor. Fix the flagged claims in the article.
- contradicted: correct the claim to match the evidence note, or remove it.
- unsupported / unverifiable: remove the specific number or detail, or soften it ("many", "typically").
- A flagged heading or title: reword it neutrally with no testing, price or percentage claim, keeping the
  target keyword (e.g. "Best Budget Hiking Boots for Beginners").
Do not add new facts. Keep every Markdown link and the overall section structure.
Output only the full revised article in Markdown."""

IMPROVE_SYSTEM = """You are the Editor. Improve the article's SEO using the listed issues.
Keep the meaning, every Markdown link and the H1. Do not invent facts, numbers or prices.
Output only the full revised article in Markdown."""


def _ok(new: str, old: str) -> bool:
    return bool(new.strip()) and len(_MD_LINK.findall(new)) >= len(_MD_LINK.findall(old)) and bool(_H1.search(new))


def _rewrite(system: str, user: str, old: str, target_words: int) -> str:
    try:
        text = llm.complete(system, [{"role": "user", "content": user}], max_tokens=min(5000, target_words * 3))
    except llm.LLMRefusal:
        return old
    text = text.strip() if isinstance(text, str) else ""
    return text if _ok(text, old) else old


_H1_LINE = re.compile(r"^#\s+(.+)$", re.MULTILINE)


def _safe_h1(body: str, keyword: str, brief_title: str) -> str:
    """Last resort: if the H1 still makes an unsupportable claim, swap in a clean title."""
    m = _H1_LINE.search(body)
    if not m or not HEADLINE_CLAIM.search(m.group(1)):
        return body
    clean = brief_title if brief_title and not HEADLINE_CLAIM.search(brief_title) else keyword.title()
    return body[:m.start(1)] + clean + body[m.end(1):]


def _format_reply(body: str, report: dict, flagged: list[dict], notes: list[str], rewrite: bool = False) -> str:
    stats = report["stats"]
    lines = [f"Article ready: {stats['words']} words, SEO score **{report['score']}/100**."]
    if rewrite:
        lines.append("This is a rewrite of one of your live articles. Say **publish it** to replace the live "
                     "version (you'll be asked to approve first).")
    if report["issues"]:
        lines.append("Remaining SEO notes: " + "; ".join(report["issues"][:3]))
    if flagged:
        lines.append(f"Fact check: {len(flagged)} claim(s) were not clearly supported and were softened or removed.")
    lines += [f"Note: {n}" for n in notes]
    return "\n".join(lines) + "\n\n---\n\n" + body


def editor(state: RankForgeState) -> dict:
    body = state.get("linked_draft") or state.get("draft") or ""
    if not body:
        return {"reply": "There is no draft to edit yet."}
    keyword = (state.get("brief") or {}).get("keyword") or state.get("keyword") or ""
    target_words = int((state.get("brief") or {}).get("word_count_target", 1200))
    flagged = state.get("flagged_claims") or []
    notes = (state.get("fact_check_notes") or []) + (state.get("link_notes") or [])

    if flagged:
        listing = "\n".join(f"- [{f['verdict']}] {f['claim']} (evidence note: {f.get('note') or 'none'})" for f in flagged)
        body = _rewrite(REVISE_SYSTEM, f"Flagged claims:\n{listing}\n\nArticle:\n{body}", body, target_words)

    body = _safe_h1(body, keyword, (state.get("brief") or {}).get("title", ""))
    report = seo_audit.seo_audit(body, keyword)
    if report["score"] < SEO_TARGET and report["issues"]:
        issues = "\n".join(f"- {i}" for i in report["issues"])
        improved = _rewrite(IMPROVE_SYSTEM, f"Target keyword: {keyword}\nTarget length: about {target_words} words\n"
                                            f"SEO issues:\n{issues}\n\nArticle:\n{body}", body, target_words)
        if improved != body:
            new_report = seo_audit.seo_audit(improved, keyword)
            if new_report["score"] >= report["score"]:
                body, report = improved, new_report

    draft_id = drafts.save_draft(state["user_id"], (state.get("brief") or {}).get("id"), "final", body,
                                 flagged_claims=flagged, seo_report=report)
    return {"final_draft": body, "seo_report": report, "draft_id": draft_id, "stage": "edited",
            "reply": _format_reply(body, report, flagged, notes, bool((state.get("brief") or {}).get("rewrite_of")))}
