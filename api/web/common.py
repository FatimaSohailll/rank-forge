"""Shared helpers for the server-rendered pages (customer app, admin, auth)."""
from pathlib import Path
from urllib.parse import urlparse

from fastapi import HTTPException, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates

from middleware import auth

templates = Jinja2Templates(directory=str(Path(__file__).resolve().parent.parent / "templates"))

# App pages load only our own script and stylesheet; no inline script, no third-party origins.
APP_HEADERS = {
    "Content-Security-Policy": "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data: https:; "
                               "connect-src 'self'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "same-origin",
    "Cache-Control": "no-store",
}


class LoginRedirect(Exception):
    """Raised by require_user; main.py turns it into a redirect to /login."""


def render(request: Request, name: str, context: dict | None = None, status_code: int = 200):
    context = {"me": auth.current_user(request), "path": request.url.path, **(context or {})}
    return templates.TemplateResponse(request, name, context, status_code=status_code, headers=APP_HEADERS)


def require_user(request: Request) -> dict:
    me = auth.current_user(request)
    if not me:
        raise LoginRedirect()
    return me


def require_admin(request: Request) -> dict:
    me = require_user(request)
    if not me.get("is_admin"):
        raise HTTPException(status_code=403, detail="Admins only")
    return me


def check_origin(request: Request) -> None:
    """CSRF defence for form posts (on top of SameSite=Lax cookies): a present Origin must be us."""
    origin = request.headers.get("origin")
    if origin and origin != "null" and urlparse(origin).netloc != request.url.netloc:
        raise HTTPException(status_code=403, detail="Cross-site request refused")


def redirect(url: str) -> RedirectResponse:
    return RedirectResponse(url, status_code=303)


def safe_next(url: str | None) -> str:
    """Only allow local redirects after login."""
    return url if url and url.startswith("/") and not url.startswith("//") else "/app"
