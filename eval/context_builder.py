"""
Build pgvector context for an alert row before passing it to the eval agent.

Searches art-chronicles and art-newsreels namespaces using the alert's
organization, alert title, and library item title as the query.
Returns formatted text ready to include in the agent's user message.

Also performs a deterministic URL-based check for newsreel presence.
"""

import asyncio
import logging
import os

log = logging.getLogger(__name__)

_NAMESPACES = ["ba:chronicles", "ba:newsreels"]
_MAX_RESULTS = 5
_EMBEDDING_MODEL = "text-embedding-3-large"

_SQL_HYBRID_NS = """
WITH semantic AS (
    SELECT dc.id, dc.content, dc.content_type,
           d.metadata->>'title' AS title, d.source_uri,
           ROW_NUMBER() OVER (ORDER BY dc.embedding <=> $1::halfvec) AS rank_sem
    FROM document_chunks dc JOIN documents d ON dc.document_id = d.id
    WHERE d.namespace = ANY($2::text[])
      AND (d.expires_at IS NULL OR d.expires_at > NOW())
    ORDER BY dc.embedding <=> $1::halfvec LIMIT $4
),
lexical AS (
    SELECT dc.id, dc.content, dc.content_type,
           d.metadata->>'title' AS title, d.source_uri,
           ROW_NUMBER() OVER (ORDER BY ts_rank_cd(dc.search_tsv, q) DESC) AS rank_lex
    FROM document_chunks dc JOIN documents d ON dc.document_id = d.id,
         plainto_tsquery('english', $3) q
    WHERE dc.search_tsv @@ q AND d.namespace = ANY($2::text[])
      AND (d.expires_at IS NULL OR d.expires_at > NOW())
    ORDER BY ts_rank_cd(dc.search_tsv, q) DESC LIMIT $4
)
SELECT COALESCE(s.id, l.id) AS id,
       COALESCE(s.content, l.content) AS content,
       COALESCE(s.content_type, l.content_type) AS content_type,
       COALESCE(s.title, l.title) AS title,
       COALESCE(s.source_uri, l.source_uri) AS source_uri,
       COALESCE(1.0 / (60 + s.rank_sem), 0) +
       COALESCE(1.0 / (60 + l.rank_lex), 0) AS rrf_score
FROM semantic s FULL OUTER JOIN lexical l ON s.id = l.id
ORDER BY rrf_score DESC LIMIT $5;
"""

_SQL_NEWSREEL_URL_CHECK = """
SELECT COUNT(*) AS cnt
FROM documents
WHERE namespace = 'ba:newsreels'
  AND source_uri = $1
  AND (expires_at IS NULL OR expires_at > NOW())
"""


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


async def _embed(text: str) -> list[float] | None:
    from openai import AsyncOpenAI
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        return None
    client = AsyncOpenAI(api_key=api_key)
    resp = await client.embeddings.create(model=_EMBEDDING_MODEL, input=[text])
    return resp.data[0].embedding


async def _search_async(query: str) -> list[dict]:
    from bubble.pgvector.client import init_pg_pool, get_pg_pool, close_pg_pool

    await init_pg_pool()
    try:
        pool = get_pg_pool()
        if pool is None:
            return []

        embedding = await _embed(query)
        if embedding is None:
            return []

        emb_str = "[" + ",".join(str(x) for x in embedding) + "]"
        rows = await pool.fetch(
            _SQL_HYBRID_NS,
            emb_str, _NAMESPACES, query, 20, _MAX_RESULTS,
        )
        return [
            {
                "title": r["title"] or "",
                "content_type": r["content_type"] or "",
                "source_uri": r["source_uri"] or "",
                "content": r["content"] or "",
            }
            for r in rows
        ]
    finally:
        await close_pg_pool()


async def _check_newsreel_url_async(url: str) -> bool:
    from bubble.pgvector.client import init_pg_pool, get_pg_pool, close_pg_pool

    await init_pg_pool()
    try:
        pool = get_pg_pool()
        if pool is None:
            return False
        row = await pool.fetchrow(_SQL_NEWSREEL_URL_CHECK, url)
        return (row["cnt"] > 0) if row else False
    finally:
        await close_pg_pool()


def check_newsreel_presence(library_item_url: str) -> bool:
    """Deterministically check if a document URL appears in art-newsreels namespace."""
    if not library_item_url or library_item_url in ("N/A", "-"):
        return False
    try:
        return asyncio.run(_check_newsreel_url_async(library_item_url))
    except Exception as e:
        log.warning("Newsreel URL check failed for %s: %s", library_item_url, e)
        return False


def fetch_context(row: dict) -> str:
    """
    Return formatted Chronicle and Newsreel context for this row,
    plus a deterministic newsreel presence signal for the library item URL.
    Returns empty string if pgvector is unavailable.
    """
    if os.environ.get("PGVECTOR_ENABLED", "").strip().lower() not in ("1", "true", "yes"):
        return ""

    query = _build_query(row)
    log.info("Fetching eval context for query: %s", query[:120])

    try:
        results = asyncio.run(_search_async(query))
    except Exception as e:
        log.warning("pgvector context fetch failed: %s", e)
        results = []

    # Deterministic newsreel URL check
    lib_url = row.get("library_item_url", "")
    newsreel_present: bool | None = None
    if lib_url and lib_url not in ("N/A", "-"):
        try:
            newsreel_present = asyncio.run(_check_newsreel_url_async(lib_url))
        except Exception as e:
            log.warning("Newsreel URL check failed: %s", e)

    lines = []

    if newsreel_present is not None:
        lines.append("## Newsreel Presence Check")
        if newsreel_present:
            lines.append(f"The document URL ({lib_url}) IS present in the art-newsreels knowledge base.")
        else:
            lines.append(f"The document URL ({lib_url}) is NOT present in the art-newsreels knowledge base.")
        lines.append("")

    if results:
        lines.append("## Reference Content (Chronicles and Newsreels)\n")
        for r in results:
            source = r.get("title") or r.get("source_uri") or "unknown"
            content_type = r.get("content_type", "")
            text = r.get("content", "")
            if text:
                lines.append(f"### [{content_type}] {source}\n{text[:2000]}\n")

    return "\n".join(lines)
