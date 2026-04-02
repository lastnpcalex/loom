"""MCP server providing web_search and web_fetch tools for local models.

Launched by Claude Code as a stdio subprocess.  Provides DuckDuckGo search
(via ddgs) and safe page fetching (via trafilatura) so that local Ollama
models get web access without needing the Anthropic API.

Usage:
    claude mcp add --transport stdio web-tools -- python mcp_web_tools.py
"""

import ipaddress
import socket
import sys
from urllib.parse import urlparse

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("web-tools")

# ---------------------------------------------------------------------------
# Safety: block internal / private URLs
# ---------------------------------------------------------------------------
_BLOCKED_NETWORKS = [
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("169.254.0.0/16"),
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("fc00::/7"),
    ipaddress.ip_network("fe80::/10"),
]

MAX_FETCH_CHARS = 100_000  # ~100 KB of extracted text


def _validate_url(url: str) -> str | None:
    """Return error message if URL is unsafe, else None."""
    try:
        parsed = urlparse(url)
    except Exception:
        return "Invalid URL"
    if parsed.scheme not in ("http", "https"):
        return f"Blocked scheme: {parsed.scheme} (only http/https allowed)"
    hostname = parsed.hostname or ""
    if not hostname:
        return "No hostname in URL"
    try:
        for _, _, _, _, sockaddr in socket.getaddrinfo(
            hostname, None, socket.AF_UNSPEC, socket.SOCK_STREAM
        ):
            ip = ipaddress.ip_address(sockaddr[0])
            for net in _BLOCKED_NETWORKS:
                if ip in net:
                    return f"{hostname} resolves to private/internal address"
    except socket.gaierror:
        return f"Cannot resolve hostname: {hostname}"
    return None


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------

@mcp.tool()
def web_search(query: str, max_results: int = 5) -> str:
    """Search the web and use the results to inform responses.

    Provides up-to-date information for current events and recent data.
    Returns search results with titles, URLs, and snippets as markdown hyperlinks.
    Use this tool for accessing information beyond your knowledge cutoff.

    Usage notes:
    - The query must be at least 2 characters
    - After answering using search results, include a "Sources:" section at the end
      of your response listing relevant URLs as markdown hyperlinks: [Title](URL)
    - Use the current year when searching for recent information or documentation
    - For GitHub URLs, prefer using the gh CLI via Bash instead
    """
    if not query.strip():
        return "No search query provided"
    max_results = max(1, min(max_results, 10))
    try:
        from ddgs import DDGS

        with DDGS() as ddgs:
            hits = list(ddgs.text(query, max_results=max_results))
        if not hits:
            return f"No results found for '{query}'"
        lines = []
        for i, h in enumerate(hits, 1):
            lines.append(f"{i}. {h.get('title', '(no title)')}")
            lines.append(f"   {h.get('href', '')}")
            lines.append(f"   {h.get('body', '')}")
        return "\n".join(lines)
    except ImportError:
        return "Error: ddgs package not installed (pip install ddgs)"
    except Exception as e:
        return f"Search error: {e}"


@mcp.tool()
def web_fetch(url: str) -> str:
    """Fetch content from a URL and extract its readable text.

    Takes a URL and returns the page's main content as clean text.
    HTTP URLs are automatically upgraded to HTTPS.
    Results may be truncated if the content is very large.
    This tool is read-only and does not modify any files.

    Usage notes:
    - The URL must be a fully-formed valid URL (public http/https only)
    - Use web_search first to find relevant pages, then web_fetch to read them
    - For GitHub URLs, prefer using the gh CLI via Bash instead
    """
    url = url.strip()
    if not url:
        return "No URL provided"
    err = _validate_url(url)
    if err:
        return f"Blocked: {err}"
    try:
        from trafilatura import fetch_url, extract

        downloaded = fetch_url(url)
        if not downloaded:
            return f"Failed to fetch: {url}"
        text = extract(downloaded, include_links=True, include_tables=True) or ""
        if not text:
            return f"No readable content extracted from: {url}"
        if len(text) > MAX_FETCH_CHARS:
            text = text[:MAX_FETCH_CHARS] + "\n\n... (truncated)"
        return text
    except ImportError:
        return "Error: trafilatura package not installed (pip install trafilatura)"
    except Exception as e:
        return f"Fetch error: {e}"


if __name__ == "__main__":
    mcp.run(transport="stdio")
