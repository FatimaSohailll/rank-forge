"""Auth & secrets: password hashing, signed session cookies, and which user the API acts as.

Rules
- A login account (has a password) or an admin can only be acted for with a valid session cookie.
  The client-supplied `user_id` is ignored when a session exists, and refused for those accounts otherwise.
- API-only users (no password, e.g. the benchmark or a script calling POST /chat) keep working with a
  `user_id` in the body; their data is still isolated by user_id.
- Secrets come only from config.settings; nothing here is ever put into graph state or a prompt.
"""
import base64
import hashlib
import hmac
import logging
import re
import secrets
import time

from fastapi import Request

from config import settings
from stores import users

log = logging.getLogger("rankforge.auth")

COOKIE = "rf_session"
ITERATIONS = 200_000
USERNAME = re.compile(r"^[a-z0-9_]{3,32}$")
MIN_PASSWORD = 8

if not settings.session_secret:
    log.warning("SESSION_SECRET not set: using a random per-process key (everyone is logged out on restart)")
_SECRET = settings.session_secret.encode() or secrets.token_bytes(32)


# ---------- passwords ----------

def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, ITERATIONS)
    return f"pbkdf2_sha256${ITERATIONS}${_b64(salt)}${_b64(digest)}"


def verify_password(password: str, stored: str | None) -> bool:
    try:
        algo, iterations, salt, digest = (stored or "").split("$")
        if algo != "pbkdf2_sha256":
            return False
        check = hashlib.pbkdf2_hmac("sha256", password.encode(), _unb64(salt), int(iterations))
        return hmac.compare_digest(check, _unb64(digest))
    except (ValueError, TypeError):
        return False


# ---------- sessions ----------

def _sign(payload: str) -> str:
    return hmac.new(_SECRET, payload.encode(), hashlib.sha256).hexdigest()


def make_session(user_id: str) -> str:
    expires = int(time.time()) + settings.session_days * 86400
    payload = f"{user_id}|{expires}"
    return f"{payload}|{_sign(payload)}"


def read_session(value: str | None) -> str | None:
    """The user_id in a valid, unexpired session cookie, else None."""
    try:
        user_id, expires, sig = (value or "").rsplit("|", 2)
        if not hmac.compare_digest(sig, _sign(f"{user_id}|{expires}")) or int(expires) < time.time():
            return None
        return user_id
    except ValueError:
        return None


def set_cookie(response, user_id: str) -> None:
    response.set_cookie(COOKIE, make_session(user_id), max_age=settings.session_days * 86400, httponly=True,
                        samesite="lax", secure=settings.cookie_secure, path="/")


def clear_cookie(response) -> None:
    response.delete_cookie(COOKIE, path="/")


def current_user(request: Request) -> dict | None:
    """The logged-in account for this request, or None."""
    user_id = read_session(request.cookies.get(COOKIE))
    if not user_id:
        return None
    user = users.get(user_id)
    if not user or user.get("disabled") or not user.get("password_hash"):
        return None
    return user


def login(user_id: str, password: str) -> dict | None:
    user = users.get(user_id.strip().lower())
    if not user or user.get("disabled") or not verify_password(password, user.get("password_hash")):
        return None
    return user


def signup(user_id: str, password: str, display_name: str = "") -> tuple[dict | None, str]:
    user_id = user_id.strip().lower()
    if not USERNAME.match(user_id):
        return None, "Username must be 3-32 characters: lowercase letters, digits or underscore."
    if len(password) < MIN_PASSWORD:
        return None, f"Password must be at least {MIN_PASSWORD} characters."
    if not users.create_account(user_id, hash_password(password), display_name.strip()[:80]):
        return None, "That username is taken."
    return users.get(user_id), ""


# ---------- who does the API act as ----------

LOGIN_REQUIRED = "This account needs you to log in first (open /login), so I can't act for it from here."
DISABLED = "This account is disabled. Contact the site admin."


def resolve_api_user(request: Request, claimed: str | None) -> tuple[str | None, str]:
    """(user_id, "") for the user the API should act as, or (None, reason)."""
    me = current_user(request)
    if me:
        return me["user_id"], ""
    claimed = (claimed or "").strip()
    if not claimed:
        return None, LOGIN_REQUIRED
    user = users.get(claimed)
    if user and user.get("disabled"):
        return None, DISABLED
    if user and (user.get("password_hash") or user.get("is_admin")):
        return None, LOGIN_REQUIRED
    return claimed, ""


def bootstrap_admin() -> None:
    """Create or update the 'admin' account from ADMIN_PASSWORD (if set)."""
    if settings.admin_password:
        users.set_password("admin", hash_password(settings.admin_password), is_admin=True)
        log.info("admin account ready")
