"""fetch_article — first engine-side tool (Track A phase 1).

Fetches a URL and returns its readable text + best-effort publication metadata
as a structured dict — replacing the line-serialized fetch the legacy pipeline
uses. Read-only: never writes topic state, evidence log, or source_db.

Reuses the proven trafilatura + httpx-fallback fetch pattern from
``mcp_servers/nrol_ao/server.py:_fetch_article_payload`` rather than inventing a
new extractor. trafilatura's own fetcher gets past bot walls that refuse plain
httpx requests (observed: 403 for httpx with a browser UA, 200 for fetch_url on
the same article); we keep that ordering.

The returned dict shape matches the architecture's A.2 spec:
    {url, headline, source, published_at, text, error}
`error` is None on success or a short string on failure (the caller decides
whether to skip the article or surface the failure).
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlparse

import httpx

# Caps the extracted text we return to the agent. The full article body can be
# large; the matcher/OBSERVE decisions only need the portion with the numeric
# values and key claims. Matches the excerpt cap the scan path already uses.
DEFAULT_MAX_CHARS = 8000
DEFAULT_TIMEOUT_SEC = 20.0
_USER_AGENT = "Mozilla/5.0 (compatible; NROL-AO engine agent)"

SCHEMA: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "fetch_article",
        "description": (
            "Fetch a news article URL and return its readable text plus "
            "best-effort publication metadata (headline, source, "
            "published_at). Use this to read an article's full body before "
            "proposing a verdict on it. Read-only — never mutates topic state."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "url": {
                    "type": "string",
                    "description": "The article URL to fetch.",
                },
                "max_chars": {
                    "type": "integer",
                    "description": (
                        "Maximum characters of extracted article text to "
                        "return. Defaults to 8000."
                    ),
                },
            },
            "required": ["url"],
            "additionalProperties": False,
        },
    },
}


def _source_from_url(url: str) -> str:
    try:
        netloc = urlparse(url).netloc.lower()
        if netloc.startswith("www."):
            netloc = netloc[4:]
        return netloc
    except Exception:
        return ""


def _fetch_html(url: str, timeout_sec: float) -> tuple[str | None, str | None]:
    """Return (html, error). Tries trafilatura.fetch_url first, httpx second."""
    try:
        import trafilatura
    except ImportError:
        return None, "trafilatura not installed"

    html: str | None = None
    try:
        html = trafilatura.fetch_url(url)
    except Exception:
        html = None
    if html:
        return html, None

    # Fallback: plain httpx with a browser-ish UA. Some sites 403 this that
    # trafilatura's fetcher gets past, but it's better than nothing.
    try:
        with httpx.Client(
            timeout=timeout_sec,
            follow_redirects=True,
            headers={"User-Agent": _USER_AGENT},
        ) as client:
            r = client.get(url)
            r.raise_for_status()
            return r.text, None
    except Exception as exc:
        return None, f"{type(exc).__name__}: {str(exc)[:180]}"


def fetch_article(
    url: str,
    *,
    max_chars: int = DEFAULT_MAX_CHARS,
    timeout_sec: float = DEFAULT_TIMEOUT_SEC,
) -> dict[str, Any]:
    """Fetch a URL and return a structured article dict.

    Always returns a dict (never raises to the agent loop) with this shape:
        {
          "url": str,            "headline": str,   "source": str,
          "published_at": str,   "text": str,       "error": str | None,
        }
    On any failure, ``error`` is set and ``text`` is empty; the caller can
    still see the URL and a best-effort source.
    """
    out: dict[str, Any] = {
        "url": url,
        "headline": "",
        "source": _source_from_url(url),
        "published_at": "",
        "text": "",
        "error": None,
    }
    if not url or not str(url).strip():
        out["error"] = "empty url"
        return out
    url = str(url).strip()
    out["url"] = url

    try:
        import trafilatura
    except ImportError:
        out["error"] = "trafilatura not installed"
        return out

    html, fetch_err = _fetch_html(url, timeout_sec)
    if not html:
        out["error"] = fetch_err or "fetch returned empty"
        return out

    # Metadata first (title, date, sitename) — trafilatura.extract_metadata
    # is cheap and tolerant of messy HTML.
    try:
        metadata = trafilatura.extract_metadata(html)
        if metadata is not None:
            title = getattr(metadata, "title", None)
            if title:
                out["headline"] = str(title).strip()
            date = getattr(metadata, "date", None)
            if date:
                out["published_at"] = str(date).strip()
            sitename = getattr(metadata, "sitename", None)
            if sitename:
                out["source"] = str(sitename).strip() or out["source"]
    except Exception:
        pass

    # Extracted readable text. include_comments=False keeps SEO junk out;
    # include_tables=True keeps the numeric values OBSERVE decisions need.
    try:
        text = trafilatura.extract(
            html, include_comments=False, include_tables=True
        ) or ""
        text = " ".join(text.split())
        if text:
            out["text"] = text[: int(max_chars)]
    except Exception as exc:
        # Extraction failed but we may still have metadata; don't lose it.
        out["error"] = f"extract: {type(exc).__name__}: {str(exc)[:180]}"

    if not out["headline"] and out["text"]:
        out["headline"] = out["text"][:160]

    if not out["text"] and not out["error"]:
        out["error"] = "extraction returned empty text"

    return out
