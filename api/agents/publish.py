"""Publish flow: a human-in-the-loop gate, then the Publisher.

publish_gate  pauses the graph with LangGraph's `interrupt()` and asks a human to approve the exact
              draft that will go live. It has NO side effects before the pause, because LangGraph
              re-runs the node from the top when the graph resumes. The approval record is created
              by graph.py when it sees the pause.
publisher     runs only after approval. It renders safe HTML, replaces affiliate links with tracked
              /r/{code} links, stores the post and marks the draft as published. No LLM involved.
"""
import json
import logging
import re
import secrets
from datetime import datetime, timezone
from urllib.parse import urlparse

from langgraph.types import interrupt
from markdown_it import MarkdownIt

from config import settings
from state import RankForgeState
from stores import affiliate, drafts, posts

log = logging.getLogger("rankforge.publish")

_H1 = re.compile(r"^#\s+(.+?)\s*#*\s*$", re.MULTILINE)
_MD_LINK = re.compile(r"\[([^\]]+)\]\((https?://[^)\s]+)\)")
_A_TAG = re.compile(r'<a href="([^"]+)">')
_markdown = MarkdownIt("commonmark", {"html": False})        # raw HTML in the article is escaped, never rendered


# ---------- pure helpers ----------

def slugify(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:80].strip("-")
    return slug or "post"


def unique_slug(base: str) -> str:
    slug, n = base, 2
    while posts.slug_exists(slug):
        slug = f"{base[:74]}-{n}"
        n += 1
    return slug


def split_title(markdown: str, fallback: str) -> tuple[str, str]:
    """(title from the first H1, markdown without that H1). The page template renders the title itself."""
    m = _H1.search(markdown)
    if not m:
        return fallback, markdown
    return m.group(1).strip(), (markdown[:m.start()] + markdown[m.end():]).lstrip("\n")


def _host(url: str) -> str:
    host = (urlparse(url).hostname or "").lower()
    return host[4:] if host.startswith("www.") else host


def _program_for(url: str, programs: list[dict]) -> dict | None:
    host = _host(url)
    for p in programs:
        ph = _host(p["base_url"])
        if ph and (host == ph or host.endswith("." + ph)):
            return p
    return None


def track_links(markdown: str, programs: list[dict]) -> tuple[str, list[dict]]:
    """Replace affiliate links with /r/{code}. Returns (markdown, [{code, target_url, anchor, program_id}])."""
    tracked: list[dict] = []

    def swap(m: re.Match) -> str:
        anchor, url = m.group(1), m.group(2)
        program = _program_for(url, programs)
        if not program:
            return m.group(0)
        code = secrets.token_urlsafe(6)
        tracked.append({"code": code, "target_url": url, "anchor": anchor, "program_id": program.get("id")})
        return f"[{anchor}](/r/{code})"
    return _MD_LINK.sub(swap, markdown), tracked


def render_html(markdown: str) -> str:
    """Markdown -> HTML with raw HTML escaped; tracked links get rel="sponsored nofollow", others "noopener"."""
    html = _markdown.render(markdown)

    def rel(m: re.Match) -> str:
        href = m.group(1)
        if href.startswith("/r/"):
            return f'<a href="{href}" rel="sponsored nofollow noopener" target="_blank">'
        if href.startswith("http"):
            return f'<a href="{href}" rel="noopener" target="_blank">'
        return m.group(0)
    return _A_TAG.sub(rel, html)


def meta_description(markdown: str, brief_meta: str = "") -> str:
    if brief_meta.strip():
        return brief_meta.strip()[:155]
    for block in re.split(r"\n\s*\n", markdown):
        text = re.sub(r"[#*_>`\[\]]|\([^)]*\)", "", block).strip()
        if len(text.split()) >= 8:
            return text[:155]
    return ""


def article_jsonld(title: str, description: str, url: str, published: str) -> dict:
    return {"@context": "https://schema.org", "@type": "Article", "headline": title[:110],
            "description": description, "datePublished": published, "dateModified": published,
            "mainEntityOfPage": {"@type": "WebPage", "@id": url}}


def public_url(slug: str) -> str:
    return f"{settings.public_site_url.rstrip('/')}/posts/{slug}"


# ---------- nodes ----------

def publish_gate(state: RankForgeState) -> dict:
    """Pause for a human decision. Returns {"approval": {...}} (and a reply when nothing is published)."""
    user_id = state["user_id"]
    draft = drafts.latest_draft(user_id, "final")
    if not draft:
        return {"approval": {"decision": "none"},
                "reply": "There's no finished article to publish yet. Ask me to write one first."}

    title, body = split_title(draft["body"], (state.get("brief") or {}).get("title") or "Untitled")
    programs = affiliate.list_programs()
    n_links = sum(1 for m in _MD_LINK.finditer(body) if _program_for(m.group(2), programs))
    report = draft.get("seo_report") or {}
    payload = {
        "action": "publish", "draft_id": draft["id"], "title": title,
        "keyword": (state.get("brief") or {}).get("keyword") or state.get("keyword") or "",
        "words": (report.get("stats") or {}).get("words") or len(body.split()),
        "seo_score": report.get("score"), "flagged_claims": len(draft.get("flagged_claims") or []),
        "affiliate_links": n_links, "slug_preview": slugify(title),
    }
    decision = interrupt(payload)                 # <- the graph stops here until a human resumes it
    decision = decision if isinstance(decision, dict) else {}
    verdict = decision.get("decision")
    if verdict in ("approve", "amend"):
        return {"approval": {"decision": verdict, "draft_id": decision.get("draft_id") or draft["id"],
                             "amend": decision.get("amend") or {}}}
    return {"approval": {"decision": "reject"},
            "reply": "Okay, I won't publish it. The draft is still saved, so just ask again whenever you're ready."}


def after_gate(state: RankForgeState) -> str:
    return "publisher" if (state.get("approval") or {}).get("decision") in ("approve", "amend") else "responder"


def publisher(state: RankForgeState) -> dict:
    user_id = state["user_id"]
    approval = state.get("approval") or {}
    draft = drafts.get_draft(user_id, approval.get("draft_id", -1))         # the exact draft the human approved
    if not draft or draft["stage"] != "final":
        return {"reply": "That draft is no longer available to publish."}
    amend = approval.get("amend") or {}

    brief = state.get("brief") or {}
    title, body = split_title(draft["body"], brief.get("title") or "Untitled")
    title = str(amend.get("title") or title).strip()[:120] or "Untitled"
    programs = affiliate.list_programs()
    body, tracked = track_links(body, programs)
    html = render_html(body)
    slug = unique_slug(slugify(str(amend.get("slug") or title)))
    description = (str(amend.get("meta_description") or "").strip()[:155]
                   or meta_description(body, brief.get("meta_description", "")))
    now = datetime.now(timezone.utc).isoformat()
    url = public_url(slug)

    post = posts.create_post(user_id, slug, title, brief.get("keyword") or state.get("keyword") or "", description,
                             html, article_jsonld(title, description, url, now))
    for link in tracked:
        posts.create_link(link["code"], user_id, post["id"], link["program_id"], link["target_url"], link["anchor"])
    drafts.set_stage(user_id, draft["id"], "published")
    log.info(json.dumps({"event": "published", "user": user_id, "post_id": post["id"], "links": len(tracked)}))

    return {"post_id": str(post["id"]), "post_url": url, "published_at": now, "stage": "published",
            "reply": f"Published: **{title}**\n{url}\n{len(tracked)} affiliate link(s) are tracked. "
                     f"Clicks and views will show up in your analytics."}
