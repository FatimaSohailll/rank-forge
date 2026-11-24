"""SERP position check (Rank Tracker tool): where does our site appear for a keyword?

Uses Tavily web search (same credit budget and cache as tools/web.py). The position is the rank among
the top `depth` results; None means "not in the top `depth`". This is an approximation of Google's
ranking (Tavily's own index), so it is labelled source='tavily' in the rankings table.
"""
from urllib.parse import urlparse

from tools import log_call
from tools import web


def _host(url: str) -> str:
    host = (urlparse(url).hostname or "").lower()
    return host[4:] if host.startswith("www.") else host


def serp_position(keyword: str, site_url: str, depth: int = 10) -> int | None:
    """1-based position of site_url's host in the results for keyword, or None. Raises ToolUnavailable."""
    site = _host(site_url)
    log_call("serp_position", {"keyword": keyword, "site": site, "depth": depth})
    for i, r in enumerate(web.web_search(keyword, max_results=depth), 1):
        host = _host(r["url"])
        if site and (host == site or host.endswith("." + site)):
            return i
    return None
