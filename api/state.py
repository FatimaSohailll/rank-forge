"""Shared LangGraph state (from RANKFORGE_CONTEXT.md) plus chat/runtime fields."""
import operator
from typing import Annotated, Literal, TypedDict

Route = Literal[
    "chat",              # general SEO/affiliate Q&A, answered by the supervisor
    "refuse",            # off-topic or prompt injection
    "keyword_research",  # -> Keyword Scout
    "write_article",     # -> Planner -> Writer -> QA -> Editor
    "publish",           # -> Publisher (always behind human approval)
    "performance",       # -> Optimizer (reads rankings + clicks)
    "remember",          # store a user preference in long-term memory
]
ROUTES: list[str] = list(Route.__args__)


class RankForgeState(TypedDict, total=False):
    # Chat / runtime
    user_id: str
    thread_id: str
    messages: Annotated[list[dict], operator.add]   # short-term memory, checkpointed per thread
    user_message: str
    route: str
    reply: str
    run_until: str
    pending_action: dict | None
    resume_token: str | None
    degraded: bool
    stage: str                  # per-turn progress marker, e.g. "planned" (reset every turn)

    # Memory
    profile: dict               # long-term: saved site profile (website, niche, audience, tone, ...)
    summary: str                # short-term: running summary of older turns
    summarised_upto: int        # messages[:summarised_upto] are folded into `summary`

    # Per-turn request details extracted by the supervisor (reset every turn)
    topic: str                  # subject/keyword the user mentioned, "" if none
    location: str               # country the user mentioned, "" if none

    # Research
    keyword: str
    keyword_meta: dict          # demand, intent, competition, commission, score (all estimates)
    keyword_candidates: list[dict]   # top scored ideas from the last research run

    # Planning
    brief: dict                 # title, outline, angle, word_count_target, affiliate_programs

    # Creation
    draft: str
    draft_id: int

    # QA
    flagged_claims: list[dict]  # [{claim, verdict, source_url, note}]
    linked_draft: str
    fact_check_notes: list[str]
    link_notes: list[str]

    # Edit
    final_draft: str
    seo_report: dict            # {score, issues, passed, stats}

    # Publish
    approval: dict              # human decision: {decision, draft_id, amend} (reset every turn)
    post_id: str
    post_url: str
    published_at: str

    # Monitor
    rankings: list[dict]
    clicks: list[dict]

    # Optimize
    optimizer_decision: str     # "rewrite" | "retarget" | "hold"
    optimizer_notes: str
    loop_count: int
