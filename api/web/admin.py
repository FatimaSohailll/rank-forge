"""Admin dashboard: analytics (spans, spend, tool budget), user management, content moderation."""
from fastapi import APIRouter, Depends, Form, HTTPException, Request

from config import settings
from middleware import cost_guard
from stores import posts, spans, users
from tools import web as web_tool
from web.common import check_origin, redirect, render, require_admin

router = APIRouter(prefix="/admin")


@router.get("")
def analytics(request: Request, me: dict = Depends(require_admin), days: int = 7):
    days = max(1, min(90, days))
    summary = spans.summary(days)
    return render(request, "admin_analytics.html", {
        "days": days, "summary": summary, "recent": spans.recent(25),
        "spend_today": cost_guard.daily_spent(), "ceiling": settings.daily_spend_ceiling_usd,
        "run_ceiling": settings.max_spend_per_run_usd, "tavily": web_tool.usage(), "model": settings.model_id,
    })


@router.get("/users")
def user_list(request: Request, me: dict = Depends(require_admin)):
    return render(request, "admin_users.html", {"users": users.list_with_stats()})


@router.post("/users/{user_id}/disabled")
def set_disabled(request: Request, user_id: str, disabled: str = Form(...), me: dict = Depends(require_admin)):
    check_origin(request)
    if user_id == me["user_id"]:
        raise HTTPException(status_code=400, detail="You can't disable yourself")
    users.set_disabled(user_id, disabled == "1")
    return redirect("/admin/users")


@router.get("/content")
def content(request: Request, me: dict = Depends(require_admin)):
    return render(request, "admin_content.html", {"posts": posts.list_all()})


@router.post("/posts/{post_id}/status")
def moderate(request: Request, post_id: int, status: str = Form(...), me: dict = Depends(require_admin)):
    check_origin(request)
    if status not in ("published", "archived") or not posts.set_status(post_id, status):
        raise HTTPException(status_code=404, detail="Post not found")
    return redirect("/admin/content")
