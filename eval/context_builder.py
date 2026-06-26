"""
Build pgvector context for an alert row before passing it to the eval agent.

Searches art-chronicles and art-newsreels namespaces using the alert's
organization, alert title, and library item title as the query.
Returns formatted text ready to include in the agent's user message.
"""

import asyncio
import logging

log = logging.getLogger(__name__)

_NAMESPACES = ["art-chronicles", "art-newsreels"]
_MAX_RESULTS = 5


def _build_query(row: dict) -> str:
    parts = []
    orgs = row.get("organization")
    if isinstance(orgs, list):
        parts.extend(orgs)
    elif isinstance(orgs, str) and orgs:
        parts.append(orgs)

    title = row.get("alert_title", "")
    if title:
        parts.append(title)

    lib_title = row.get("library_item_preliminary_title")
    if isinstance(lib_title, dict):
        t = lib_title.get("title", "")
        if t and t not in ("N/A", "-"):
            parts.append(t)
    elif isinstance(lib_title, str) and lib_title not in ("N/A", "-", ""):
        parts.append(lib_title)

    return " ".join(parts) if parts else row.get("alert_type", "NAIC regulatory change")


async def _search_async(query: str) -> list[dict]:
    from bubble.pgvector.client import init_pg_pool, close_pg_pool
    from bubble.pgvector.search_tool import set_pgvector_namespaces, search_knowledge_base

    await init_pg_pool()
    try:
        set_pgvector_namespaces(_NAMESPACES)
        raw = await search_knowledge_base(query=query, max_results=_MAX_RESULTS)
        if isinstance(raw, list):
            return raw
        return []
    finally:
        await close_pg_pool()


def fetch_context(row: dict) -> str:
    """
    Return a formatted string of Chronicle and Newsreel context for this row.
    Returns empty string if pgvector is unavailable or search fails.
    """
    import os
    if os.environ.get("PGVECTOR_ENABLED", "").strip().lower() not in ("1", "true", "yes"):
        return ""

    query = _build_query(row)
    log.info("Fetching eval context for query: %s", query[:100])

    try:
        results = asyncio.run(_search_async(query))
    except Exception as e:
        log.warning("pgvector context fetch failed: %s", e)
        return ""

    if not results:
        return ""

    lines = ["## Reference Content (Chronicles and Newsreels)\n"]
    for r in results:
        source = r.get("source") or r.get("filename") or "unknown"
        text = r.get("text") or r.get("content") or ""
        if text:
            lines.append(f"### {source}\n{text[:2000]}\n")

    return "\n".join(lines)
