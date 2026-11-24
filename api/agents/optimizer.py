"""Optimizer: reads ranking + click signals per post and decides rewrite | retarget | hold.

The decision is deterministic and explainable (no LLM, no cost), so the same numbers always give the
same advice and the benchmark can check it. Two modes:

- route "performance": report only (every post, its signals, and the recommendation).
- route "optimize":    act on one post. Re-write loop -> Content Planner -> Writer -> QA -> Editor (the new
                       draft replaces the live post after human approval). Re-target loop -> Keyword Scout
                       (finds a lower-competition variant). Every decision is written to `action_log`.

Loop guard: a post rewritten `max_loops` times is not rewritten again automatically; the Optimizer holds
and asks a human to decide (human-in-the-loop "loop overflow").
"""
import logging

from config import settings
from state import RankForgeState
from stores import metrics, posts

log = logging.getLogger("rankforge.optimizer")

MIN_DAYS = 7                     # fewer days of ranking data than this: too early to judge
STUCK_BAND = (10, 30)            # page 2-3
OUT_OF_REACH = 40                # worse than this, with little traffic: re-target
LOW_DAILY_SESSIONS = 50
SLIP_POSITIONS = 5               # positions lost week over week
TRAFFIC_DROP = 0.25              # share of sessions lost week over week
PRIORITY = {"retarget": 0, "rewrite": 1, "hold": 2}


def decide(rank: dict | None, clicks: dict | None, rewrite_count: int = 0,
           max_loops: int | None = None) -> dict:
    """{decision, reason, escalate}. Positions are 1 = top; higher is worse."""
    max_loops = settings.max_loops if max_loops is None else max_loops
    rank, clicks = rank or {}, clicks or {}
    recent, prior = rank.get("recent_position"), rank.get("prior_position")
    s7, sp7 = clicks.get("sessions_7d", 0), clicks.get("sessions_prev_7d", 0)
    traffic_drop = (sp7 - s7) / sp7 if sp7 else 0.0

    if recent is None:
        if sp7 and traffic_drop >= TRAFFIC_DROP:
            decision, reason = "rewrite", f"visits fell {traffic_drop:.0%} week over week (no ranking data yet)"
        else:
            decision, reason = "hold", "not enough ranking data yet"
    elif rank.get("days_tracked", 0) < MIN_DAYS:
        decision, reason = "hold", f"only {rank.get('days_tracked', 0)} day(s) of ranking data; too early to judge"
    elif recent > OUT_OF_REACH and s7 / 7 < LOW_DAILY_SESSIONS and (prior is None or prior - recent < 3):
        decision, reason = "retarget", (f"ranks around #{recent:.0f} with ~{s7 / 7:.0f} visits/day and is not "
                                        "climbing; this keyword looks out of reach, so target an easier variant")
    elif prior is not None and recent - prior >= SLIP_POSITIONS:
        decision, reason = "rewrite", f"slipped from #{prior:.0f} to #{recent:.0f}; refresh and strengthen the article"
    elif sp7 and traffic_drop >= TRAFFIC_DROP:
        decision, reason = "rewrite", f"visits fell {traffic_drop:.0%} week over week"
    elif STUCK_BAND[0] < recent <= STUCK_BAND[1] and (prior is None or abs(recent - prior) < 3):
        decision, reason = "rewrite", (f"stuck around #{recent:.0f} (page 2-3); a stronger, more complete "
                                       "version could reach the top 10")
    elif prior is not None and prior - recent >= 3:
        decision, reason = "hold", f"improving (#{prior:.0f} to #{recent:.0f}); leave it alone"
    elif recent <= STUCK_BAND[0]:
        decision, reason = "hold", f"in the top 10 (#{recent:.0f}); leave it alone"
    else:
        decision, reason = "hold", f"stable around #{recent:.0f}"

    escalate = decision != "hold" and rewrite_count >= max_loops
    if escalate:
        reason += (f". It has already been rewritten {rewrite_count} time(s) (limit {max_loops}), so I won't "
                   "change it automatically; a human should decide")
        decision = "hold"
    return {"decision": decision, "reason": reason, "escalate": escalate}


def assess(user_id: str, rankings: list[dict], clicks: list[dict]) -> list[dict]:
    """Every published post of the user with its signals and decision, most urgent first."""
    rk = {r["post_id"]: r for r in rankings or []}
    ck = {c["post_id"]: c for c in clicks or []}
    rows = []
    for p in posts.list_for_user(user_id):
        if p["status"] != "published":
            continue
        d = decide(rk.get(p["id"]), ck.get(p["id"]), p.get("rewrite_count") or 0)
        rows.append({"post_id": p["id"], "title": p["title"], "slug": p["slug"], "keyword": p["keyword"],
                     "rewrite_count": p.get("rewrite_count") or 0, "rank": rk.get(p["id"]) or {},
                     "clicks": ck.get(p["id"]) or {}, **d})
    rows.sort(key=lambda r: (PRIORITY[r["decision"]], -int(r["escalate"])))
    return rows


def _line(r: dict) -> str:
    rank, clicks = r["rank"], r["clicks"]
    bits = []
    if rank.get("recent_position") is not None:
        was = f" (was #{rank['prior_position']:.0f})" if rank.get("prior_position") is not None else ""
        bits.append(f"position #{rank['recent_position']:.0f}{was}")
    if clicks:
        bits.append(f"{clicks.get('sessions_7d', 0)} visits and {clicks.get('affiliate_clicks_7d', 0)} affiliate "
                    f"clicks in 7 days (CTR {clicks.get('ctr_7d', 0):.1%})")
    signals = "; ".join(bits) or "no traffic data yet"
    action = "needs your decision" if r["escalate"] else r["decision"]
    return f"- **{r['title']}**: {signals}. **{action}**: {r['reason']}."


def _report(rows: list[dict]) -> str:
    lines = ["Here's how your published articles are doing (last 7 days vs the week before):"]
    lines += [_line(r) for r in rows]
    if any(r["decision"] != "hold" for r in rows):
        lines.append("\nSay **optimize** (or e.g. \"rewrite the tent article\") and I'll act on the top "
                     "recommendation. A rewrite replaces the live post only after you approve it.")
    lines.append("_Demo data: this report includes simulated rankings, visits, and affiliate clicks; these are not real performance results._"
                 if any((r["rank"].get("has_simulated") or r["rank"].get("source") == "simulated") for r in rows) else "")
    return "\n".join(l for l in lines if l)


def _pick(rows: list[dict], state: RankForgeState) -> dict | None:
    target = state.get("target_post_id") or 0
    if target:
        return next((r for r in rows if r["post_id"] == target), None)
    topic = (state.get("topic") or "").lower().strip()
    if topic:
        words = {w for w in topic.split() if len(w) > 3}
        best = max(rows, key=lambda r: len(words & set(f"{r['title']} {r['keyword']}".lower().split())), default=None)
        if best and words & set(f"{best['title']} {best['keyword']}".lower().split()):
            return best
    return next((r for r in rows if r["decision"] != "hold" or r["escalate"]), None)


def optimizer(state: RankForgeState) -> dict:
    user_id = state["user_id"]
    rows = assess(user_id, state.get("rankings") or [], state.get("clicks") or [])
    if not rows:
        return {"optimizer_decision": "hold", "reply": "You don't have any published articles yet, so there's "
                                                       "nothing to track. Ask me to write one first."}
    if state.get("route") != "optimize":
        return {"optimizer_decision": "hold", "reply": _report(rows)}

    r = _pick(rows, state)
    if r is None:
        reply = ("I couldn't find that article among your published posts." if state.get("target_post_id")
                 else "All your articles are holding steady, so there's nothing to change right now.\n\n" + _report(rows))
        return {"optimizer_decision": "hold", "reply": reply}

    loop_count = r["rewrite_count"] + (1 if r["decision"] == "rewrite" else 0)
    metrics.log_action(user_id, r["post_id"], r["decision"], r["reason"], loop_count)
    log.info("optimizer %s post=%s: %s", r["decision"], r["post_id"], r["reason"])
    notes = f"**{r['title']}** {r['reason']}."
    if r["decision"] == "rewrite":
        return {"optimizer_decision": "rewrite", "optimizer_notes": notes, "keyword": r["keyword"],
                "rewrite_of": r["post_id"], "loop_count": loop_count, "topic": ""}
    if r["decision"] == "retarget":
        return {"optimizer_decision": "retarget", "optimizer_notes": notes, "topic": r["keyword"],
                "loop_count": loop_count}
    prefix = "Human review needed: " if r["escalate"] else "No change needed: "
    return {"optimizer_decision": "hold", "optimizer_notes": notes, "reply": prefix + notes}


def after_optimizer(state: RankForgeState) -> str:
    if state.get("route") == "optimize":
        if state.get("optimizer_decision") == "rewrite":
            return "content_planner"
        if state.get("optimizer_decision") == "retarget":
            return "keyword_scout"
    return "responder"


def run_for_all_users() -> dict:
    """Cron: assess every user's posts and log one decision per post (no rewrites without a human)."""
    from agents import click_tracker, rank_tracker
    user_ids = sorted({p["user_id"] for p in posts.list_all() if p["status"] == "published"})
    counts = {"rewrite": 0, "retarget": 0, "hold": 0, "escalated": 0}
    for uid in user_ids:
        rows = assess(uid, rank_tracker.summarise(metrics.rankings_for_user(uid)),
                      click_tracker.summarise(metrics.clicks_for_user(uid)))
        for r in rows:
            metrics.log_action(uid, r["post_id"], r["decision"], ("[cron] " + r["reason"]), r["rewrite_count"])
            counts[r["decision"]] += 1
            counts["escalated"] += r["escalate"]
    return {"users": len(user_ids), **counts}
