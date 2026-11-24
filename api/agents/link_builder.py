"""Link Builder: injects affiliate links into the draft.

The model only *chooses* anchors (phrases already in the article) and a search query per link.
The injection itself is deterministic: links are placed by code, never rewritten by the model,
so the article text cannot be altered here. The Publisher later rewrites these links to tracked /r/{code} links.
"""
import copy
import re
from urllib.parse import quote_plus

import llm
from state import RankForgeState
from stores import affiliate, drafts

MAX_LINKS = 5
DISCLOSURE = ("*Disclosure: this article contains affiliate links. We may earn a commission if you buy "
              "through them, at no extra cost to you.*")

SYSTEM_PROMPT = f"""You place affiliate links in an article. Choose up to {MAX_LINKS} natural anchor phrases
that appear VERBATIM in the article text (a product type or product name a reader might buy, 1-5 words).
Never pick text from headings. For each, pick the best program from the list and a short product search
query. Only use programs from the list."""
SCHEMA = {
    "type": "object",
    "properties": {"links": {"type": "array", "items": {
        "type": "object",
        "properties": {"anchor": {"type": "string"}, "program": {"type": "string"}, "query": {"type": "string"}},
        "required": ["anchor", "program", "query"], "additionalProperties": False}}},
    "required": ["links"], "additionalProperties": False,
}
_MD_LINK = re.compile(r"\[[^\]]*\]\([^)]*\)")


def resolve_program(label, programs: dict[str, dict]) -> dict | None:
    """Find the catalog program a model-returned label refers to.

    Models sometimes echo a whole listing line ("REI Co-op (AvantLink, 5.0% commission)") instead of the
    bare name, so fall back to the longest catalog name the label starts with.
    """
    label = str(label or "").strip()
    if label in programs:
        return programs[label]
    lowered = label.lower()
    for name in sorted(programs, key=len, reverse=True):
        if lowered == name.lower() or lowered.startswith(name.lower()):
            return programs[name]
    return None


def _schema_for(program_names: list[str]) -> dict:
    """SCHEMA with `program` restricted to the allowed program names."""
    schema = copy.deepcopy(SCHEMA)
    schema["properties"]["links"]["items"]["properties"]["program"] = {"type": "string", "enum": program_names}
    return schema


def inject_links(body: str, links: list[dict], programs: dict[str, dict]) -> tuple[str, int]:
    """Replace the first plain-text occurrence of each anchor with a Markdown link. Returns (body, n)."""
    applied = 0
    lines = body.split("\n")
    for link in links[:MAX_LINKS]:
        anchor, program = str(link.get("anchor", "")).strip(), resolve_program(link.get("program"), programs)
        if not anchor or not program:
            continue
        url = program["base_url"].replace("{q}", quote_plus(str(link.get("query") or anchor)[:80]))
        for n, line in enumerate(lines):
            if line.lstrip().startswith("#"):
                continue
            spans = [m.span() for m in _MD_LINK.finditer(line)]
            start = line.find(anchor)
            while start >= 0 and any(a <= start < b for a, b in spans):
                start = line.find(anchor, start + 1)
            if start >= 0:
                lines[n] = f"{line[:start]}[{anchor}]({url}){line[start + len(anchor):]}"
                applied += 1
                break
    return "\n".join(lines), applied


def link_builder(state: RankForgeState) -> dict:
    draft = state.get("draft") or ""
    brief = state.get("brief") or {}
    catalog = {p["name"]: p for p in affiliate.list_programs()}
    chosen = {n: catalog[n] for n in brief.get("affiliate_programs", []) if n in catalog}
    if not draft or not chosen:
        return {"linked_draft": draft, "link_notes": ["No affiliate programs matched this article."]}

    program_list = "\n".join(f"- {n} ({p['network']}, {p['commission_pct']}% commission)" for n, p in chosen.items())
    schema = _schema_for(list(chosen))
    try:
        out = llm.complete(SYSTEM_PROMPT + "\nThe program field must be exactly one of the program names, without the details in brackets.",
                           [{"role": "user", "content": f"Programs:\n{program_list}\n\nArticle:\n{draft}"}],
                           max_tokens=500, schema=schema)
        links = out.get("links") or []
    except llm.LLMRefusal:
        links = []
    body, applied = inject_links(draft, links, chosen)
    if applied and DISCLOSURE not in body:
        body += "\n\n" + DISCLOSURE
    notes = [] if applied else ["No natural anchor phrases were found for the affiliate programs."]
    drafts.save_draft(state["user_id"], brief.get("id"), "linked", body)
    return {"linked_draft": body, "link_notes": notes}
