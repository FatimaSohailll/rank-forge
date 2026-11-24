"""Rate limiter: in-process sliding windows (per user and per client IP).

Protects the LLM key's spend limit and the Tavily free tier from a single client hammering /chat.
In-process only: with several workers each has its own window (fine for one container).
"""
import threading
import time
from collections import defaultdict, deque

from fastapi import Request

from config import settings

WINDOW_S = 60.0
_lock = threading.Lock()
_hits: dict[str, deque] = defaultdict(deque)


def allow(key: str, limit: int, window: float = WINDOW_S) -> bool:
    """Record a hit for key; False if the key already used `limit` hits in the window."""
    now = time.monotonic()
    with _lock:
        q = _hits[key]
        while q and now - q[0] > window:
            q.popleft()
        if len(q) >= limit:
            return False
        q.append(now)
        return True


def client_ip(request: Request) -> str:
    # Behind a proxy (HF Spaces, Render) the first X-Forwarded-For entry is the client.
    forwarded = request.headers.get("x-forwarded-for", "")
    return forwarded.split(",")[0].strip() or (request.client.host if request.client else "unknown")


def allow_chat(request: Request, user_id: str) -> bool:
    return (allow(f"ip:{client_ip(request)}", settings.chat_per_minute_ip)
            and allow(f"user:{user_id}", settings.chat_per_minute_user))


def allow_login(request: Request) -> bool:
    return allow(f"login:{client_ip(request)}", settings.login_per_minute_ip)


RATE_LIMITED = "You're sending requests too quickly. Please wait a minute and try again."


def reset() -> None:
    """Test helper."""
    with _lock:
        _hits.clear()
