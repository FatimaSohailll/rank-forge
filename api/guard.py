"""Zero-cost safety prefilter. Runs before any LLM call.

Catches obvious prompt injection and clearly off-topic requests with regexes.
Anything subtler is handled by the supervisor's system prompt (second line of defense).
"""
import re

INJECTION_PATTERNS = [
    r"\b(ignore|disregard|forget|override)\b.{0,40}\b(previous|prior|above|earlier|all|your|the)\b.{0,20}\b(instructions?|rules|prompts?|directives|guidelines)\b",
    r"\b(reveal|show|print|repeat|output|leak|tell me)\b.{0,30}\b(system|hidden|initial|original)\s+(prompt|instructions?|message)\b",
    r"\byou are (now|no longer)\b",
    r"\b(developer|god|admin|debug|jailbreak|dan)\s+mode\b",
    r"\bdo anything now\b",
    r"\bpretend (to be|you are|you're)\b",
    r"</?\s*(system|instructions?|admin)\s*>",
    r"\[\s*(system|admin)\s*\]",
    r"\b(api[_ ]?key|secret key|env(ironment)? variables?|database password|connection string)\b",
    r"\b(other|another|all) users?'?s?\b.{0,30}\b(data|posts|keywords|memory|niche|articles|info)\b",
]

OFF_TOPIC_PATTERNS = [
    r"\b(write|compose|make)\b.{0,25}\b(poem|song|lyrics|haiku|limerick|rap)\b",
    r"\btell me a joke\b",
    r"\b(solve|calculate|compute|integrate|differentiate)\b.{0,30}\b(equation|integral|derivative|matrix|x\s*=)",
    r"\b(do|write|solve|finish)\b.{0,15}\bmy (homework|assignment|exam)\b",
    r"\bwrite\b.{0,25}\b(python|javascript|java|c\+\+|c#|sql|rust|go)\b.{0,20}\b(code|script|program|function|class)\b",
    r"\b(diagnose|prescribe)\b|\bmedical advice\b",
    r"\b(which|what) stocks? should i (buy|sell)\b",
    r"\bhow (do|can) i (hack|make a bomb|steal)\b",
]

_injection = [re.compile(p, re.IGNORECASE) for p in INJECTION_PATTERNS]
_off_topic = [re.compile(p, re.IGNORECASE) for p in OFF_TOPIC_PATTERNS]

INJECTION_REPLY = (
    "I can't follow instructions that try to change my rules or reach other users' data or "
    "system settings. I'm RankForge, your SEO and affiliate content assistant: I can research "
    "keywords, write and fact-check articles, add affiliate links, publish posts and track how "
    "they perform. What would you like to work on?"
)

OFF_TOPIC_REPLY = (
    "That's outside what I do. I'm RankForge, an SEO and affiliate content assistant. I can "
    "find keywords worth targeting, plan and write articles, add affiliate links, publish to "
    "your blog and analyse rankings and clicks. Want to start with keyword ideas for your niche?"
)


def check(message: str) -> tuple[str, str] | None:
    """Return (kind, reply) if the message must be refused, else None."""
    for pattern in _injection:
        if pattern.search(message):
            return "injection", INJECTION_REPLY
    for pattern in _off_topic:
        if pattern.search(message):
            return "off_topic", OFF_TOPIC_REPLY
    return None
