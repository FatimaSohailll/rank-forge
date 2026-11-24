"""Cron endpoint for the Monitor lane (call it daily from a scheduler, e.g. GitHub Actions or cron-job.org).

POST /cron/run  with header  X-Cron-Secret: <CRON_SECRET>
  1. Rank Tracker: live SERP position for a few published posts (Tavily credits, capped per run).
  2. Optimizer: assess every user's posts and log a decision per post to action_log.
     It never rewrites or publishes on its own; acting needs the user (and their approval).
"""
import hmac
import logging
import uuid

from fastapi import APIRouter, Header, HTTPException

import run_context
from agents import optimizer, rank_tracker
from config import settings

log = logging.getLogger("rankforge.cron")
router = APIRouter(prefix="/cron")


@router.post("/run")
def run(x_cron_secret: str = Header(default="")):
    if settings.cron_secret in ("", "change-me"):
        raise HTTPException(status_code=503, detail="Set CRON_SECRET to enable cron endpoints")
    if not hmac.compare_digest(x_cron_secret, settings.cron_secret):
        raise HTTPException(status_code=401, detail="Bad cron secret")
    ctx = run_context.RunContext(run_id=uuid.uuid4().hex, user_id="cron", thread_id="cron")
    token = run_context.set_run(ctx)
    try:
        ranks = rank_tracker.track_live()
        decisions = optimizer.run_for_all_users()
    finally:
        run_context.reset_run(token)
    log.info("cron run: ranks=%s decisions=%s", ranks, decisions)
    return {"rank_tracker": ranks, "optimizer": decisions, "tavily_searches": ctx.searches}
