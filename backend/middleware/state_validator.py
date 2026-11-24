"""State validator: checks every node's output against RankForgeState before it is merged.

Invalid fields are dropped (never passed forward) and reported with their exact path, so a bad or
manipulated model output cannot silently corrupt downstream agents.
"""
import logging
from urllib.parse import urlparse

from pydantic import TypeAdapter, ValidationError

from state import ROUTES, RankForgeState

log = logging.getLogger("rankforge.validator")

try:
    _adapter = TypeAdapter(RankForgeState)
except Exception:                                    # very old Python/pydantic combos: custom rules only
    log.warning("schema validation unavailable", exc_info=True)
    _adapter = None

DECISIONS = {"", "rewrite", "retarget", "hold"}


def _custom(update: dict) -> list[tuple[str, str]]:
    problems = []
    if "route" in update and update["route"] not in ROUTES + [""]:
        problems.append(("route", f"unknown route {update['route']!r}"))
    if update.get("post_url"):
        parsed = urlparse(str(update["post_url"]))
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            problems.append(("post_url", "not an http(s) URL"))
    if "loop_count" in update and isinstance(update["loop_count"], int) and update["loop_count"] < 0:
        problems.append(("loop_count", "must be >= 0"))
    if "optimizer_decision" in update and update["optimizer_decision"] not in DECISIONS:
        problems.append(("optimizer_decision", f"unknown decision {update['optimizer_decision']!r}"))
    report = update.get("seo_report")
    if isinstance(report, dict) and "score" in report and not (0 <= (report.get("score") or 0) <= 100):
        problems.append(("seo_report.score", "must be 0-100"))
    return problems


def check(node: str, update) -> tuple[dict, list[str]]:
    """(cleaned update, problems). Problems look like 'seo_report.score: must be 0-100'."""
    if update is None:
        return {}, []
    if not isinstance(update, dict):
        return {}, [f"<root>: node returned {type(update).__name__}, expected dict"]
    bad: dict[str, str] = {}
    if _adapter is not None:
        try:
            _adapter.validate_python(update)
        except ValidationError as e:
            for err in e.errors():
                loc = [str(p) for p in err["loc"]]
                bad.setdefault(loc[0], f"{'.'.join(loc)}: {err['msg']}")
    for field, msg in _custom(update):
        bad.setdefault(field.split(".")[0], f"{field}: {msg}")
    if not bad:
        return update, []
    for msg in bad.values():
        log.error("state validation failed in %s: %s", node, msg)
    return {k: v for k, v in update.items() if k not in bad}, list(bad.values())
