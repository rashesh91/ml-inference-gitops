import logging

import httpx

log = logging.getLogger(__name__)


async def search(query: str) -> str:
    """Search DuckDuckGo instant answers API (no API key required)."""
    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            resp = await client.get(
                "https://api.duckduckgo.com/",
                params={"q": query, "format": "json", "no_html": "1", "skip_disambig": "1"},
            )
            data = resp.json()

        # Prefer AbstractText (Wikipedia summary) or RelatedTopics
        if data.get("AbstractText"):
            return data["AbstractText"][:500]

        if data.get("Answer"):
            return data["Answer"]

        topics = data.get("RelatedTopics", [])
        snippets = []
        for t in topics[:3]:
            if isinstance(t, dict) and t.get("Text"):
                snippets.append(t["Text"][:150])
        if snippets:
            return " | ".join(snippets)

        return f"No direct answer found for: {query}"

    except Exception as e:
        log.warning(f"Search failed for '{query}': {e}")
        return f"Search unavailable: {e}"
