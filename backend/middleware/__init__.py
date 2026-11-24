"""Middleware layers from CONTEXT.md, outermost first:

observability   -> spans per node (observability.py)
auth & secrets  -> sessions, password hashing, who the API acts as (auth.py); secrets only in config.py
rate limiter    -> per-user / per-IP sliding windows (rate_limit.py)
cost guard      -> per-run and daily LLM spend ceilings (cost_guard.py), checked before every LLM call
retry/fallback  -> Anthropic SDK retries with backoff (llm.py), one Tavily retry (tools/web.py),
                   tools degrade instead of crashing (tools.ToolUnavailable)
state validator -> Pydantic check of every node's output (state_validator.py)
human-in-loop   -> publish approval, loop cap (agents/publish.py, agents/optimizer.py, graph.py)
"""
