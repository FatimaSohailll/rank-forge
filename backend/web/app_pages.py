"""Customer pages: dashboard, article generator, my articles, article workspace.

Server-rendered (Jinja2). The generator and the workspace publish button talk to the same JSON API as
any client (/chat/stream, /approvals, /resume) using the session cookie, so the UI gets no extra powers.
Every read is scoped to the logged-in user.
"""
from fastapi import APIRouter, Depends, Form, HTTPException, Request

from agents import click_tracker, optimizer, rank_tracker
from agents.publish import public_url, render_html
from stores import approvals, drafts, keywords, metrics, posts
from web.common import check_origin, redirect, render, require_user

router = APIRouter(prefix="/app")


def _signals(user_id: str) -> tuple[list[dict], dict]:
    """Optimizer view of every published post + 7-day totals (no LLM, no search credits)."""
    ranks = rank_tracker.summarise(metrics.rankings_for_user(user_id))
    clicks = click_tracker.summarise(metrics.clicks_for_user(user_id))
    rows = optimizer.assess(user_id, ranks, clicks)
    s7 = sum(c["sessions_7d"] for c in clicks)
    c7 = sum(c["affiliate_clicks_7d"] for c in clicks)
    totals = {"sessions_7d": s7, "sessions_prev_7d": sum(c["sessions_prev_7d"] for c in clicks),
              "affiliate_clicks_7d": c7, "conversions_7d": sum(c["conversions_7d"] for c in clicks),
              "ctr_7d": (c7 / s7) if s7 else 0.0}
    return rows, totals


@router.get("")
def dashboard(request: Request, me: dict = Depends(require_user)):
    uid = me["user_id"]
    rows, totals = _signals(uid)
    my_posts = posts.list_for_user(uid)
    return render(request, "dashboard.html", {
        "rows": rows, "totals": totals,
        "published": sum(1 for p in my_posts if p["status"] == "published"),
        "finals": drafts.list_drafts(uid, "final", limit=5),
        "keywords": keywords.list_keywords(uid, limit=8),
        "pending": approvals.list_pending(uid),
        "actions": metrics.actions_for_user(uid, limit=8),
    })


@router.get("/generator")
def generator(request: Request, me: dict = Depends(require_user), optimize: int = 0, topic: str = ""):
    post = posts.get_for_user(me["user_id"], optimize) if optimize else None
    # Recover a saved draft after refresh or a server restart without inferring it from chat text.
    saved = drafts.latest_draft(me["user_id"], "final") if not post and not topic else None
    saved_article, saved_preview = None, ""
    if saved:
        from agents.publish import split_title
        title, body = split_title(saved["body"], "Your saved article")
        report = saved.get("seo_report") or {}
        saved_article = {"draft_id": saved["id"], "title": title,
                         "words": (report.get("stats") or {}).get("words") or len(body.split()),
                         "seo_score": report.get("score"), "workspace_url": f"/app/articles/{saved['id']}"}
        saved_preview = render_html(body)
    return render(request, "generator.html", {"optimize_post": post, "topic": topic[:120],
                                               "saved_article": saved_article, "saved_preview": saved_preview})


@router.get("/articles")
def articles(request: Request, me: dict = Depends(require_user)):
    uid = me["user_id"]
    stats = {c["post_id"]: c for c in click_tracker.summarise(metrics.clicks_for_user(uid))}
    ranks = {r["post_id"]: r for r in rank_tracker.summarise(metrics.rankings_for_user(uid))}
    return render(request, "articles.html", {
        "posts": posts.list_for_user(uid), "stats": stats, "ranks": ranks,
        "finals": drafts.list_drafts(uid, "final"),
        "published_drafts": drafts.list_drafts(uid, "published", limit=20),
        "public_url": public_url,
    })


@router.get("/articles/{draft_id}")
def workspace(request: Request, draft_id: int, me: dict = Depends(require_user)):
    uid = me["user_id"]
    draft = drafts.get_draft(uid, draft_id)                      # owner-only
    if not draft:
        raise HTTPException(status_code=404, detail="Draft not found")
    from agents.publish import brief_for, split_title
    brief = brief_for(uid, draft)
    title, body = split_title(draft["body"], brief.get("title") or "Untitled")
    pending = [a for a in approvals.list_pending(uid) if (a["action"] or {}).get("draft_id") == draft_id]
    replaces = posts.get_for_user(uid, int(brief["rewrite_of"])) if brief.get("rewrite_of") else None
    return render(request, "workspace.html", {
        "draft": draft, "brief": brief, "title": title, "preview": render_html(body),
        "report": draft.get("seo_report") or {}, "flagged": draft.get("flagged_claims") or [],
        "pending": pending[0] if pending else None, "replaces": replaces,
    })


@router.post("/posts/{post_id}/status")
def set_post_status(request: Request, post_id: int, status: str = Form(...), me: dict = Depends(require_user)):
    """Unpublish (archive) or restore one of your own posts."""
    check_origin(request)
    if status not in ("published", "archived") or not posts.set_status(post_id, status, user_id=me["user_id"]):
        raise HTTPException(status_code=404, detail="Post not found")
    return redirect("/app/articles")
