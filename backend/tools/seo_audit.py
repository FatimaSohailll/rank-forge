"""Local, deterministic SEO audit (no network, no LLM).

seo_audit(article, target_keyword) -> {score 0..100, issues, passed, stats}
Accepts Markdown or HTML. Each check carries a weight; weights sum to 100.
"""
import re

import textstat

from tools import log_call

_TAG = re.compile(r"<[^>]+>")
_MD_HEADING = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$", re.MULTILINE)
_HTML_HEADING = re.compile(r"<h([1-6])[^>]*>(.*?)</h\1>", re.IGNORECASE | re.DOTALL)
_LINKS = re.compile(r"\[[^\]]+\]\([^)]+\)|<a\s[^>]*href=", re.IGNORECASE)
_WORD = re.compile(r"[A-Za-z0-9'’-]+")


def _headings(article: str) -> list[tuple[int, str]]:
    found = [(len(m.group(1)), m.group(2).strip()) for m in _MD_HEADING.finditer(article)]
    found += [(int(m.group(1)), _TAG.sub("", m.group(2)).strip()) for m in _HTML_HEADING.finditer(article)]
    return found


def _plain(article: str) -> str:
    text = _TAG.sub(" ", article)
    text = re.sub(r"^#{1,6}\s+", "", text, flags=re.MULTILINE)
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)
    return re.sub(r"[*_`>]", "", text)


def seo_audit(article: str, target_keyword: str) -> dict:
    log_call("seo_audit", {"target_keyword": target_keyword, "chars": len(article)})
    kw = target_keyword.strip().lower()
    headings = _headings(article)
    text = _plain(article)
    words = _WORD.findall(text)
    n_words = len(words)
    lower_text = text.lower()

    h1s = [h for lvl, h in headings if lvl == 1]
    h2s = [h for lvl, h in headings if lvl == 2]
    kw_count = lower_text.count(kw) if kw else 0
    density = (kw_count * len(kw.split()) / n_words * 100) if n_words else 0.0
    first_100 = " ".join(words[:100]).lower()
    paragraphs = [p for p in re.split(r"\n\s*\n", text) if len(p.split()) > 5]
    avg_para = (sum(len(p.split()) for p in paragraphs) / len(paragraphs)) if paragraphs else 0
    flesch = textstat.flesch_reading_ease(text) if n_words >= 30 else 0.0
    n_links = len(_LINKS.findall(article))

    score = 0
    issues: list[str] = []
    passed: list[str] = []

    def check(ok: bool, weight: int, good: str, bad: str, partial: float = 0.0) -> None:
        nonlocal score
        if ok:
            score += weight
            passed.append(good)
        else:
            score += int(weight * partial)
            issues.append(bad)

    check(any(kw in h.lower() for h in h1s), 15, "Target keyword is in the H1 title.",
          f"Put the target keyword '{target_keyword}' in the H1 title.")
    check(len(h1s) == 1, 5, "Exactly one H1.", f"Use exactly one H1 (found {len(h1s)}).")
    check(kw in first_100, 10, "Keyword appears in the first 100 words.",
          "Mention the target keyword in the first 100 words.")
    check(0.5 <= density <= 2.5, 15, f"Keyword density {density:.1f}% is healthy.",
          f"Keyword density is {density:.1f}%; aim for 0.5-2.5%.", partial=0.4 if 0 < density < 4 else 0)
    check(n_words >= 800, 15, f"Length is good ({n_words} words).",
          f"Article is {n_words} words; aim for 800+.", partial=0.5 if n_words >= 500 else 0)
    check(len(h2s) >= 3, 10, f"{len(h2s)} H2 sections give clear structure.",
          f"Add H2 sections (found {len(h2s)}, want 3+).", partial=0.5 if len(h2s) >= 2 else 0)
    check(any(kw in h.lower() for h in h2s), 5, "Keyword (or variant) appears in an H2.",
          "Use the target keyword in at least one H2.")
    check(flesch >= 50, 10, f"Readable (Flesch {flesch:.0f}).",
          f"Hard to read (Flesch {flesch:.0f}); use shorter sentences.", partial=0.5 if flesch >= 30 else 0)
    check(n_links >= 2, 10, f"{n_links} links found.", f"Add at least 2 links (found {n_links}).",
          partial=0.5 if n_links == 1 else 0)
    check(0 < avg_para <= 120, 5, "Paragraphs are a good length.",
          "Break up long paragraphs (aim for under 120 words each).")

    return {
        "score": min(100, score),
        "issues": issues,
        "passed": passed,
        "stats": {"words": n_words, "keyword_density_pct": round(density, 2), "h1": len(h1s),
                  "h2": len(h2s), "links": n_links, "flesch": round(flesch, 1)},
    }
