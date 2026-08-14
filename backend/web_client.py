"""Web research for ATLAS — keyless search (DuckDuckGo) + lightweight page reader.

No API key required, so research works out of the box. The chat agent drives it:
web_search to gather options, web_read to check details, then it synthesizes a
recommendation.
"""
import re
import html as _html


def _ddgs():
    try:
        from ddgs import DDGS            # maintained package name
    except ImportError:
        from duckduckgo_search import DDGS   # older name, fallback
    return DDGS


def search(query: str, count: int = 6) -> list[dict]:
    DDGS = _ddgs()
    out = []
    with DDGS() as d:
        for r in d.text(query, max_results=count):
            out.append({"title": r.get("title"), "url": r.get("href") or r.get("url"),
                        "snippet": r.get("body")})
    return out


def read(url: str, max_chars: int = 3000) -> str:
    """Fetch a page and return cleaned, truncated main text."""
    import requests
    r = requests.get(url, timeout=12,
                     headers={"User-Agent": "Mozilla/5.0 (ATLAS research bot)"})
    text = r.text
    text = re.sub(r"(?is)<(script|style|noscript).*?</\1>", " ", text)
    text = re.sub(r"(?s)<[^>]+>", " ", text)
    text = _html.unescape(text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:max_chars]
