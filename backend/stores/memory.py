"""Long-term memory: a small per-user profile, keyed by user_id (table `user_memory`).

Only the allowlisted keys below can be stored (from the product canvas). Passwords, API keys and
personal contact details are never stored: `set_item` rejects them, whatever the caller. Postgres
when available, in-process dict otherwise (dev/tests only). Every query filters by user_id.
"""
import re

import guard
from stores import db

KEYS = ("website", "niche", "audience", "tone", "location", "content_preferences")
MAX_VALUE = 200

_mem: dict[str, dict[str, str]] = {}

_SENSITIVE = [
    ("an email address", re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")),
    ("a phone number", re.compile(r"(?<!\d)(?:\+?\d[\s().-]?){9,}\d(?!\d)")),
    ("a card or ID number", re.compile(r"\b(?:\d[ -]?){13,16}\b")),
    ("a secret", re.compile(r"\b(password|passwd|passcode|api[_ -]?key|secret|token|private key|ssn|"
                            r"social security|credit card|cvv)\b", re.IGNORECASE)),
    ("a key or token", re.compile(r"\b(?:sk-[\w-]{10,}|tvly-[\w-]{8,}|ghp_\w{10,}|AKIA[0-9A-Z]{12,}|"
                                  r"eyJ[\w-]{20,}|[A-Za-z0-9_-]{32,})\b")),
]


class RejectedValue(ValueError):
    """The value or key may not be stored."""


def check_value(key: str, value: str) -> str:
    """Return the cleaned value or raise RejectedValue (unknown key, too long, sensitive, injection)."""
    if key not in KEYS:
        raise RejectedValue(f"'{key}' is not something I store")
    value = " ".join(str(value).split())
    if not value:
        raise RejectedValue("empty value")
    if len(value) > MAX_VALUE:
        raise RejectedValue("too long")
    for label, pattern in _SENSITIVE:
        if pattern.search(value):
            raise RejectedValue(f"it looks like {label}")
    verdict = guard.check(value)
    if verdict and verdict[0] == "injection":
        raise RejectedValue("it looks like an instruction, not a preference")
    return value


def get_profile(user_id: str) -> dict[str, str]:
    """The user's saved profile, allowlisted keys only (legacy rows with other keys are ignored)."""
    if db.enabled():
        with db.pool.connection() as conn:
            rows = conn.execute("select key, value from user_memory where user_id=%s and key = any(%s)",
                                (user_id, list(KEYS))).fetchall()
        return {r["key"]: r["value"] for r in rows}
    return dict(_mem.get(user_id, {}))


def set_item(user_id: str, key: str, value: str) -> str:
    """Save one profile item (validated). Returns the stored value."""
    value = check_value(key, value)
    if db.enabled():
        with db.pool.connection() as conn:
            conn.execute(
                "insert into user_memory (user_id, key, value) values (%s,%s,%s)"
                " on conflict (user_id, key) do update set value=excluded.value, updated_at=now()",
                (user_id, key, value))
    else:
        _mem.setdefault(user_id, {})[key] = value
    return value


def delete_items(user_id: str, keys: list[str]) -> list[str]:
    """Forget items. Returns the keys that existed and were removed."""
    keys = [k for k in keys if k in KEYS]
    existing = [k for k in keys if k in get_profile(user_id)]
    if not existing:
        return []
    if db.enabled():
        with db.pool.connection() as conn:
            conn.execute("delete from user_memory where user_id=%s and key = any(%s)", (user_id, existing))
    else:
        for k in existing:
            _mem.get(user_id, {}).pop(k, None)
    return existing


def clear_memory() -> None:
    """Test helper for the in-process fallback."""
    _mem.clear()
