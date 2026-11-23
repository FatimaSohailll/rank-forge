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

    # Research
    keyword: str
    keyword_meta: dict          # volume, difficulty, cpc, affiliate_commission

    # Planning
    brief: dict                 # title, outline, angle, word_count_target, affiliate_programs

    # Creation
    draft: str

    # QA
    flagged_claims: list[dict]  # [{claim, verdict, source_url}]
    linked_draft: str

    # Edit
    final_draft: str

    # Publish
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
