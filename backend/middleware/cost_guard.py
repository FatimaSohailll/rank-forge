"""Cost guard: per-run and daily LLM spend ceilings, checked before every LLM call.

`check()` runs in llm.complete before the request is sent, so a retry or a later node can never push
a run over budget. When a ceiling is hit it raises CostCeilingHit; the observability wrapper stops the
node, marks the run as halted, and the responder tells the user. Progress is already checkpointed.
Today's spend is loaded from the `spans` table once per day, so it survives restarts.
"""
import logging
import threading
from datetime import datetime, timezone

from config import settings

log = logging.getLogger("rankforge.cost")

_lock = threading.Lock()
_daily = {"day": None, "base": 0.0, "added": 0.0}

HALTED_REPLY = ("I stopped this request because it reached the spending limit ({reason}). Nothing was "
                "published, and finished steps are saved. Please try again later.")


class CostCeilingHit(Exception):
    """A spend ceiling was reached; the run must stop."""


def _today():
    return datetime.now(timezone.utc).date()


def daily_spent() -> float:
    with _lock:
        if _daily["day"] != _today():
            base = 0.0
            try:
                from stores import spans
                base = spans.spend_today()
            except Exception:                        # never block a call because the stats query failed
                log.warning("could not load today's spend", exc_info=True)
            _daily.update(day=_today(), base=base, added=0.0)
        return _daily["base"] + _daily["added"]


def check(run_cost: float) -> None:
    """Raise CostCeilingHit if this run or today is at its ceiling."""
    if run_cost >= settings.max_spend_per_run_usd:
        raise CostCeilingHit(f"per-request limit of ${settings.max_spend_per_run_usd:.2f}")
    spent = daily_spent()
    if spent >= settings.daily_spend_ceiling_usd:
        raise CostCeilingHit(f"daily limit of ${settings.daily_spend_ceiling_usd:.2f}")
    if spent >= settings.daily_spend_ceiling_usd * settings.cost_warn_at_pct / 100:
        log.warning("daily spend at %.0f%% of ceiling", 100 * spent / settings.daily_spend_ceiling_usd)


def record(cost: float) -> None:
    daily_spent()                                    # roll the day if needed
    with _lock:
        _daily["added"] += cost


def reset() -> None:
    """Test helper."""
    with _lock:
        _daily.update(day=None, base=0.0, added=0.0)
