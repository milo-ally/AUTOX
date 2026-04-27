"""Web tools — fetch and search."""

from __future__ import annotations

from typing import Any

import httpx


def run_web_fetch(input_data: dict[str, Any]) -> dict[str, Any]:
    """Fetch a URL and extract readable text content."""
    url = input_data.get("url", "")

    if not url:
        return {"error": "missing required field 'url'"}

    try:
        with httpx.Client(follow_redirects=True, timeout=30.0) as client:
            response = client.get(url, headers={"User-Agent": "microclaw/0.1.0"})
            response.raise_for_status()

        content_type = response.headers.get("content-type", "")
        body = response.text

        # Try to extract readable content with readability
        try:
            from readability import Document
            from bs4 import BeautifulSoup

            doc = Document(body)
            summary_html = doc.summary()
            soup = BeautifulSoup(summary_html, "lxml")
            text = soup.get_text(separator="\n", strip=True)
        except ImportError:
            # Fallback: strip HTML tags with BeautifulSoup alone
            try:
                from bs4 import BeautifulSoup
                soup = BeautifulSoup(body, "lxml")
                text = soup.get_text(separator="\n", strip=True)
            except ImportError:
                text = body[:5000]

        # Truncate if too long
        max_chars = 50_000
        if len(text) > max_chars:
            text = text[:max_chars] + "\n... [content truncated]"

        return {
            "url": url,
            "content_type": content_type,
            "text": text,
            "status": "ok",
        }
    except httpx.HTTPError as e:
        return {"error": f"HTTP error fetching {url}: {e}"}
    except Exception as e:
        return {"error": str(e)}


def run_web_search(input_data: dict[str, Any]) -> dict[str, Any]:
    """Search the web for current information.

    Uses a simple approach: if no search API is configured, returns
    a message suggesting the user configure a search provider.
    """
    query = input_data.get("query", "")
    if not query:
        return {"error": "missing required field 'query'"}

    # Try using SearXNG if configured via environment
    searx_url = _get_searxng_url()
    if searx_url:
        return _search_searxng(searx_url, query)

    # Fallback: use DuckDuckGo HTML search
    return _search_duckduckgo(query)


def _get_searxng_url() -> str | None:
    """Check if a SearXNG instance is configured."""
    import os
    return os.environ.get("SEARXNG_URL") or os.environ.get("SEARX_URL")


def _search_searxng(base_url: str, query: str) -> dict[str, Any]:
    """Search using a SearXNG instance."""
    try:
        with httpx.Client(follow_redirects=True, timeout=15.0) as client:
            response = client.get(
                f"{base_url.rstrip('/')}/search",
                params={"q": query, "format": "json"},
                headers={"User-Agent": "microclaw/0.1.0"},
            )
            response.raise_for_status()
            data = response.json()

        results = []
        for item in data.get("results", [])[:10]:
            results.append({
                "title": item.get("title", ""),
                "url": item.get("url", ""),
                "snippet": item.get("content", ""),
            })

        return {
            "query": query,
            "results": results,
            "total": len(results),
        }
    except Exception as e:
        return {"error": f"SearXNG search failed: {e}"}


def _search_duckduckgo(query: str) -> dict[str, Any]:
    """Search using DuckDuckGo HTML endpoint."""
    try:
        with httpx.Client(follow_redirects=True, timeout=15.0) as client:
            response = client.get(
                "https://html.duckduckgo.com/html/",
                params={"q": query},
                headers={"User-Agent": "microclaw/0.1.0"},
            )
            response.raise_for_status()

        from bs4 import BeautifulSoup
        soup = BeautifulSoup(response.text, "lxml")

        results = []
        for result_div in soup.select(".result"):
            title_el = result_div.select_one(".result__title a")
            snippet_el = result_div.select_one(".result__snippet")
            if title_el:
                results.append({
                    "title": title_el.get_text(strip=True),
                    "url": title_el.get("href", ""),
                    "snippet": snippet_el.get_text(strip=True) if snippet_el else "",
                })
            if len(results) >= 10:
                break

        return {
            "query": query,
            "results": results,
            "total": len(results),
        }
    except Exception as e:
        return {"error": f"Web search failed: {e}"}
