"""Single entry point for every Claude call (claude-haiku-5-5 only).

Cost controls: thinking disabled, effort low, small max_tokens per call site, and the cost guard
(middleware/cost_guard.py) is checked BEFORE every call. Every call records its usage on the current
RunContext (the /chat `tokens` field, observability spans, and the daily spend).
Retry & fallback: the SDK retries timeouts, 429 and 5xx with exponential backoff (max_retries=3).
"""
import json
import logging
import re

import anthropic

from config import settings
import run_context
from middleware import cost_guard

log = logging.getLogger("rankforge.llm")

client = anthropic.Anthropic(
    api_key=settings.anthropic_api_key or None,
    timeout=90.0,   # a full article is a long generation
    max_retries=3,  # exponential backoff with jitter on timeouts, 429, 5xx
)

# Feature flags flipped off once if the API rejects them, so we never fail twice.
_use_disabled_thinking = True
_use_structured_output = True


class LLMRefusal(Exception):
    pass


def _cost(tokens_in: int, tokens_out: int) -> float:
    return (tokens_in * settings.price_in_per_mtok + tokens_out * settings.price_out_per_mtok) / 1_000_000


def _parse_json(text: str) -> dict:
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if match:
            return json.loads(match.group(0))
        raise


def complete(system: str, messages: list[dict], max_tokens: int = 400,
             schema: dict | None = None) -> str | dict:
    """Call the model. Returns text, or a dict when `schema` (JSON Schema) is given."""
    global _use_disabled_thinking, _use_structured_output

    output_config: dict = {"effort": "low"}
    if schema and _use_structured_output:
        output_config["format"] = {"type": "json_schema", "schema": schema}

    params: dict = {
        "model": settings.model_id,
        "max_tokens": max_tokens,
        "system": system,
        "messages": messages,
        "output_config": output_config,
    }
    if _use_disabled_thinking:
        params["thinking"] = {"type": "disabled"}

    ctx = run_context.current()
    cost_guard.check(ctx.cost_usd)                   # raises CostCeilingHit before any spend
    try:
        response = client.messages.create(**params)
    except anthropic.BadRequestError as e:
        msg = str(e).lower()
        retry = False
        if "thinking" in msg and _use_disabled_thinking:
            _use_disabled_thinking = False
            retry = True
        if ("format" in msg or "output_config" in msg) and _use_structured_output:
            _use_structured_output = False
            retry = True
        if not retry:
            raise
        log.warning("retrying LLM call without unsupported option: %s", e)
        return complete(system, messages, max_tokens, schema)

    usage = response.usage
    cost = _cost(usage.input_tokens, usage.output_tokens)
    ctx.add_usage(usage.input_tokens, usage.output_tokens, cost)
    cost_guard.record(cost)

    if response.stop_reason == "refusal":
        raise LLMRefusal("model declined the request")

    text = "".join(b.text for b in response.content if b.type == "text").strip()
    if schema:
        return _parse_json(text)
    return text
