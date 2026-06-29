"""
Build pgvector context for an alert row before passing it to the eval agent.

Three context sources:
1. Semantic search across ba:chronicles and ba:newsreels — topic coverage and
   whether the event/document was mentioned in published newsreel articles.
2. Semantic search across newsreel-generation:ART — actual ingested document
   content (meeting materials, transcripts) for factual field verification.
3. Filename presence check in newsreel-generation:ART — deterministic signal
   for whether the library item was ingested into the newsreel backend.

Also extracts Bubble ground truth (agenda chronicle topics) from bubble_action
if present on the row.
"""

import asyncio
import logging
import os

log = logging.getLogger(__name__)

_CHRONICLE_NEWSREEL_NS = ["ba:chronicles", "ba:newsreels"]
_BACKEND_NS = ["newsreel-generation:ART"]
_MAX_RESULTS = 6
_EMBEDDING_MODEL = "text-embedding-3-large"

_SQL_HYBRID = """
WITH semantic AS (
    SELECT dc.id, dc.content, dc.content_type,
           d.metadata->>'title' AS title, d.source_uri, d.namespace,
           ROW_NUMBER() OVER (ORDER BY dc.embedding <=> $1::halfvec) AS rank_sem
    FROM document_chunks dc JOIN documents d ON dc.document_id = d.id
    WHERE d.namespace = ANY($2::text[])
      AND (d.expires_at IS NULL OR d.expires_at > NOW())
    ORDER BY dc.embedding <=> $1::halfvec LIMIT $4
),
lexical AS (
    SELECT dc.id, dc.content, dc.content_type,
           d.metadata->>'title' AS title, d.source_uri, d.namespace,
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
       COALESCE(s.namespace, l.namespace) AS namespace,
       COALESCE(1.0 / (60 + s.rank_sem), 0) +
       COALESCE(1.0 / (60 + l.rank_lex), 0) AS rrf_score
FROM semantic s FULL OUTER JOIN lexical l ON s.id = l.id
ORDER BY rrf_score DESC LIMIT $5;
"""

_SQL_FILENAME_CHECK = """
SELECT COUNT(*) AS cnt
FROM documents
WHERE namespace = 'newsreel-generation:ART'
  AND metadata->>'title' ILIKE $1
  AND (expires_at IS NULL OR expires_at > NOW())
"""


def _clean_org(org: str) -> str:
    return org.removeprefix("NEW ORGANIZATION: ").strip()


def _build_query(row: dict) -> str:
    parts = []
    orgs = row.get("organization")
    if isinstance(orgs, list):
        parts.extend(_clean_org(o) for o in orgs if o)
    elif isinstance(orgs, str) and orgs:
        parts.append(_clean_org(orgs))

    title = row.get("alert_title", "")
    if title:
        parts.append(title)

    event_title = row.get("event_title", "")
    if event_title and event_title not in ("N/A", "-", "") and event_title != title:
        parts.append(event_title)

    event_date = row.get("event_start_date_time", "")
    if event_date and event_date not in ("N/A", "-", ""):
        parts.append(event_date[:10])

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


async def _search_async(query: str, namespaces: list[str], n: int) -> list[dict]:
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
        rows = await pool.fetch(_SQL_HYBRID, emb_str, namespaces, query, n * 2, n)
        return [
            {
                "title": r["title"] or "",
                "content_type": r["content_type"] or "",
                "source_uri": r["source_uri"] or "",
                "namespace": r["namespace"] or "",
                "content": r["content"] or "",
            }
            for r in rows
        ]
    finally:
        await close_pg_pool()


async def _check_filename_async(filename: str) -> bool:
    from bubble.pgvector.client import init_pg_pool, get_pg_pool, close_pg_pool

    await init_pg_pool()
    try:
        pool = get_pg_pool()
        if pool is None:
            return False
        row = await pool.fetchrow(_SQL_FILENAME_CHECK, filename)
        return (row["cnt"] > 0) if row else False
    finally:
        await close_pg_pool()


def _extract_bubble_ground_truth(row: dict) -> str:
    """Extract ground truth data from bubble_action — agenda chronicle topics."""
    ba = row.get("bubble_action")
    if not ba or not isinstance(ba, dict):
        return ""

    lines = []

    agenda_previews = ba.get("agenda_item_previews", [])
    if agenda_previews:
        lines.append("## Bubble Ground Truth: Agenda Items and Chronicle Topics")
        lines.append("These are the agenda items and their assigned chronicle topics as recorded in Bubble:")
        for item in agenda_previews:
            agenda_title = item.get("title", "")
            topics = item.get("chronicle_topics", [])
            if agenda_title:
                topic_str = ", ".join(topics) if topics else "no topics assigned"
                lines.append(f"- **{agenda_title}**: {topic_str}")
        lines.append("")

    # Pull any topic fields from event/library item enrichment
    for preview_key, label in [("event_preview", "Event"), ("library_item_preview", "Library Item")]:
        preview = ba.get(preview_key, {})
        fields = preview.get("fields", {}) if isinstance(preview, dict) else {}
        for k, v in fields.items():
            if "topic" in k.lower() or "chronicle" in k.lower():
                lines.append(f"## Bubble Ground Truth: {label} {k}")
                lines.append(str(v))
                lines.append("")

    return "\n".join(lines)


def _get_org_tree() -> str:
    try:
        from bubble.org_tree import get_org_tree
        return get_org_tree()
    except Exception:
        return ""


def fetch_context(row: dict) -> str:
    """
    Return formatted context for the eval agent:
    - Org tree (for verifying organization field accuracy)
    - Bubble ground truth (agenda topics from bubble_action)
    - Filename presence check in newsreel-generation:ART
    - Semantic search results from ba:chronicles + ba:newsreels
    - Semantic search results from newsreel-generation:ART

    Returns empty string if pgvector is unavailable.
    """
    if os.environ.get("PGVECTOR_ENABLED", "").strip().lower() not in ("1", "true", "yes"):
        return ""

    query = _build_query(row)
    log.info("Fetching eval context for query: %s", query[:120])

    # Parallel async work
    async def _noop() -> None:
        return None

    async def _gather():
        cn_task = _search_async(query, _CHRONICLE_NEWSREEL_NS, _MAX_RESULTS)
        backend_task = _search_async(query, _BACKEND_NS, 4)
        filename = row.get("library_items_file_name", "")
        filename_check_task = (
            _check_filename_async(filename)
            if filename and filename not in ("N/A", "-", "")
            else _noop()
        )
        return await asyncio.gather(cn_task, backend_task, filename_check_task,
                                    return_exceptions=True)

    try:
        cn_results, backend_results, filename_found = asyncio.run(_gather())
    except Exception as e:
        log.warning("pgvector context fetch failed: %s", e)
        cn_results, backend_results, filename_found = [], [], None

    if isinstance(cn_results, Exception):
        cn_results = []
    if isinstance(backend_results, Exception):
        backend_results = []
    if isinstance(filename_found, Exception):
        filename_found = None

    lines = []

    # 0. Org tree — for verifying organization field accuracy
    org_tree = _get_org_tree()
    if org_tree:
        lines.append("## Reference: Valid NAIC Organization Names")
        lines.append("Use this to verify that the `organization` field contains exact, valid org names from the Bubble hierarchy:\n")
        lines.append(org_tree)
        lines.append("")

    # 1. Bubble ground truth
    bubble_gt = _extract_bubble_ground_truth(row)
    if bubble_gt:
        lines.append(bubble_gt)

    # 2. Newsreel backend presence check (filename-based)
    filename = row.get("library_items_file_name", "")
    if filename and filename not in ("N/A", "-", ""):
        lines.append("## Newsreel Backend Presence Check")
        if filename_found:
            lines.append(
                f"**FOUND**: The file \"{filename}\" IS present in the newsreel-generation backend "
                f"(newsreel-generation:ART). This document was ingested for newsreel creation."
            )
        else:
            lines.append(
                f"**NOT FOUND**: The file \"{filename}\" was NOT found in the newsreel-generation backend. "
                f"It may not have been ingested, or the filename may differ slightly."
            )
        lines.append("")

    # 3. Chronicle and newsreel article context
    if cn_results:
        lines.append("## Reference Context: ART Chronicles and Newsreel Articles\n")
        lines.append("Use this to evaluate newsreel relevance and chronicle topic accuracy:\n")
        for r in cn_results:
            ns = r.get("namespace", "")
            source = r.get("title") or r.get("source_uri") or "unknown"
            ns_label = "Newsreel Article" if "newsreel" in ns else "Chronicle"
            text = r.get("content", "")
            if text:
                lines.append(f"### [{ns_label}] {source}\n{text[:1500]}\n")

    # 4. Backend document content (factual verification)
    if backend_results:
        lines.append("## Reference Context: Ingested Document / Transcript Content\n")
        lines.append("Use this to verify factual fields (titles, agenda items, descriptions):\n")
        for r in backend_results:
            source = r.get("title") or "unknown"
            text = r.get("content", "")
            if text:
                lines.append(f"### {source}\n{text[:1500]}\n")

    return "\n".join(lines)
