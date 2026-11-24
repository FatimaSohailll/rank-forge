"""Public, unauthenticated pages: published articles, tracked affiliate redirects, sitemap, robots.

Article HTML is produced by agents/publish.py with raw HTML escaped, and is served with a strict
Content-Security-Policy as a second layer, because the text was written by an LLM.
"""
import logging
import re
from datetime import date, datetime
from pathlib import Path
from xml.sax.saxutils import escape

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates

from agents.publish import public_url
from config import settings
from stores import posts

log = logging.getLogger("rankforge.web")
router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).resolve().parent.parent / "templates"))

SLUG = re.compile(r"^[a-z0-9][a-z0-9-]{0,99}$")
CODE = re.compile(r"^[A-Za-z0-9_-]{4,32}$")
PAGE_HEADERS = {
    "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; img-src https: data:; "
                               "base-uri 'none'; form-action 'none'; frame-ancestors 'none'",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "strict-origin-when-cross-origin",
}


def _day(value) -> str:
    return value.strftime("%B %d, %Y") if isinstance(value, (datetime, date)) else str(value)[:10]


@router.get("/posts/{slug}", response_class=HTMLResponse)
def post_page(slug: str, request: Request):
    post = posts.get_published(slug) if SLUG.match(slug) else None
    if not post:
        raise HTTPException(status_code=404, detail="Post not found")
    try:
        posts.record_view(post["user_id"], post["id"])         # rough page-view counter (bots included)
    except Exception:
        log.warning("view count failed", exc_info=True)
    return templates.TemplateResponse(request, "post.html", {
        "post": post, "canonical": public_url(post["slug"]), "jsonld": post.get("schema_jsonld"),
        "published": _day(post["published_at"]),
    }, headers=PAGE_HEADERS)


@router.get("/r/{code}")
def affiliate_redirect(code: str):
    """Log the click, then send the reader to the affiliate URL stored for this code."""
    link = posts.get_link(code) if CODE.match(code) else None
    if not link or not str(link["target_url"]).startswith(("http://", "https://")):
        raise HTTPException(status_code=404, detail="Unknown link")
    try:
        posts.record_click(link["user_id"], link["post_id"])
    except Exception:
        log.warning("click count failed", exc_info=True)
    return RedirectResponse(link["target_url"], status_code=302,
                            headers={"Cache-Control": "no-store", "X-Robots-Tag": "noindex, nofollow"})


def _iso_day(value) -> str:
    return value.strftime("%Y-%m-%d") if hasattr(value, "strftime") else str(value)[:10]


@router.get("/sitemap.xml")
def sitemap() -> Response:
    rows = "".join(f"<url><loc>{escape(public_url(p['slug']))}</loc><lastmod>{_iso_day(p['updated_at'])}</lastmod></url>"
                   for p in posts.list_published())
    xml = f'<?xml version="1.0" encoding="UTF-8"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">{rows}</urlset>'
    return Response(xml, media_type="application/xml")


@router.get("/robots.txt")
def robots() -> Response:
    base = settings.public_site_url.rstrip("/")
    return Response(f"User-agent: *\nAllow: /posts/\nDisallow: /r/\nSitemap: {base}/sitemap.xml\n",
                    media_type="text/plain")
