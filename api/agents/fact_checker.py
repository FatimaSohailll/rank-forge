"""Fact Checker: extracts verifiable claims, checks each against live web evidence.

Cost is bounded: at most `fact_check_max_claims` claims (default 3) -> one Tavily search each, plus
two LLM calls. Headings are linted for unsupportable claims without any search or LLM call.
Web evidence is untrusted data. Claims that are not clearly supported are flagged for the Editor.
"""
import logging
import re

import llm
from config import settings
from state import RankForgeState
from tools import ToolUnavailable
from tools import web

log = logging.getLogger("rankforge.fact_checker")

VERDICTS = ["supported", "unsupported", "contradicted", "unverifiable"]


def extract_system(max_claims: int) -> str:
    return ("You extract checkable factual claims from an article: specific numbers, prices, "
            "specifications, dates, named studies or rankings. Skip opinions and general advice. "
            f"Return at most {max_claims} of the most specific and risky claims, each quoted or "
            "closely paraphrased from the article.")


# Titles and headings are where writers slip in claims nobody can back up ("Tested", "Under $100").
# This check is deterministic: no LLM call and no search credit.
HEADLINE_CLAIM = re.compile(
    r"(?:\b(?:tested|lab[- ]tested|hands[- ]on|we (?:tried|tested|reviewed|ranked)|reviewed by|"
    r"expert[- ](?:approved|picked)|number one|best[- ]selling|guaranteed|proven)\b)"
    r"|(?:#\s?1\b)|(?:[$£€]\s?\d)|(?:\bunder\s+(?:[$£€]\s?)?\d)|(?:\b\d+\s?%)",
    re.IGNORECASE,
)


def headline_flags(draft: str) -> list[dict]:
    """Flag headings that claim testing, prices, percentages or rankings we cannot support."""
    flagged = []
    for line in draft.splitlines():
        if line.lstrip().startswith("#"):
            heading = line.lstrip("# ").strip()
            if HEADLINE_CLAIM.search(heading):
                flagged.append({"claim": heading, "verdict": "unsupported", "source_url": "",
                                "note": "Heading claims testing, a price, a percentage or a ranking we cannot "
                                        "back up. Reword it neutrally and keep the target keyword."})
    return flagged


EXTRACT_SCHEMA = {"type": "object", "properties": {"claims": {"type": "array", "items": {"type": "string"}}},
                  "required": ["claims"], "additionalProperties": False}

VERIFY_SYSTEM = f"""You verify claims using only the evidence provided for each claim.
{web.UNTRUSTED_NOTICE}
Verdicts: supported (evidence clearly backs it), contradicted (evidence says otherwise),
unsupported (evidence is on topic but does not back it), unverifiable (no usable evidence).
Give the best source_url from that claim's evidence (or "") and a short note."""
VERIFY_SCHEMA = {
    "type": "object",
    "properties": {"verdicts": {"type": "array", "items": {
        "type": "object",
        "properties": {"index": {"type": "integer"}, "verdict": {"type": "string", "enum": VERDICTS},
                       "source_url": {"type": "string"}, "note": {"type": "string"}},
        "required": ["index", "verdict", "source_url", "note"], "additionalProperties": False}}},
    "required": ["verdicts"], "additionalProperties": False,
}


def fact_checker(state: RankForgeState) -> dict:
    draft = state.get("draft") or ""
    if not draft:
        return {"flagged_claims": [], "fact_check_notes": []}
    notes: list[str] = []
    headline = headline_flags(draft)
    max_claims = settings.fact_check_max_claims

    try:
        out = llm.complete(extract_system(max_claims), [{"role": "user", "content": draft}], max_tokens=500,
                           schema=EXTRACT_SCHEMA)
        claims = [str(c).strip()[:250] for c in (out.get("claims") or []) if str(c).strip()][:max_claims]
    except llm.LLMRefusal:
        return {"flagged_claims": headline, "fact_check_notes": ["Claim extraction was declined."]}
    if not claims:
        return {"flagged_claims": headline, "fact_check_notes": []}

    blocks, evidence_urls = [], []
    for i, claim in enumerate(claims):
        try:
            results = web.web_search(claim, max_results=3)
        except ToolUnavailable:
            results = []
            notes.append("Web search was unavailable for some claims.")
        evidence_urls.append({r["url"] for r in results})
        text = "\n".join(f"- {r['url']}\n{r['snippet']}" for r in results) or "(no evidence found)"
        blocks.append(f"Claim {i}: {claim}\nEvidence:\n{text}")

    try:
        out = llm.complete(VERIFY_SYSTEM, [{"role": "user", "content": "\n\n".join(blocks)}], max_tokens=900,
                           schema=VERIFY_SCHEMA)
        verdicts = {v.get("index"): v for v in (out.get("verdicts") or []) if isinstance(v, dict)}
    except llm.LLMRefusal:
        verdicts = {}

    flagged = []
    for i, claim in enumerate(claims):
        v = verdicts.get(i, {})
        verdict = v.get("verdict") if v.get("verdict") in VERDICTS else "unverifiable"
        if verdict == "supported":
            continue
        url = v.get("source_url", "")
        flagged.append({"claim": claim, "verdict": verdict,
                        "source_url": url if url in evidence_urls[i] else "",
                        "note": str(v.get("note", ""))[:200]})
    return {"flagged_claims": headline + flagged, "fact_check_notes": sorted(set(notes))}
