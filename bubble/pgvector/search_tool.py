"""``search_knowledge_base`` — pgvector semantic search tool.

An optional alternative (or complement) to OpenAI's ``FileSearchTool``.
Returns ranked chunks from the ``document_chunks`` table joined with
``documents`` metadata.

Activation: set ``enable_pgvector_search_tool = true`` in the chat config.
"""

from __future__ import annotations

import contextvars
import json
import os
import re
from datetime import datetime
from typing import Any, Optional

from agents import function_tool

from bubble.pgvector.client import get_pg_pool
from bubble.pgvector.tool_cache import get_cached, set_cached

# ContextVar set per-request before the agent runs.
# Value is the user's org namespace string (e.g. "user:abc123") or None.
_org_namespace: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    "org_namespace", default=None
)

# Static namespaces this chat is allowed to search (from ChatConfig.pgvector_namespaces).
# Empty list = legacy behaviour (search all non-org_upload content).
_pgvector_namespaces: contextvars.ContextVar[list[str]] = contextvars.ContextVar(
    "pgvector_namespaces", default=[]
)


def set_org_namespace(namespace: Optional[str]) -> None:
    _org_namespace.set(namespace)


def get_org_namespace() -> Optional[str]:
    return _org_namespace.get()


def set_pgvector_namespaces(namespaces: list[str]) -> None:
    _pgvector_namespaces.set(namespaces)


def get_pgvector_namespaces() -> list[str]:
    return _pgvector_namespaces.get()


# Pattern for "ART Newsreel | February 26, 2026" or "February 26, 2026" etc.
_DATE_IN_QUERY_PATTERN = re.compile(
    r"(?:"
    r"(\d{4})-(\d{2})-(\d{2})"  # ISO: 2026-02-26
    r"|"
    r"(January|February|March|April|May|June|July|August|September|October|November|December)\s+"
    r"(\d{1,2}),?\s+(\d{4})"  # February 26, 2026
    r"|"
    r"(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\.?\s+"
    r"(\d{1,2}),?\s+(\d{4})"  # Feb 26, 2026
    r")",
    re.I,
)

# ---------------------------------------------------------------------------
# SQL
# ---------------------------------------------------------------------------

_SQL_HYBRID_ALL = """
WITH semantic AS (
    SELECT dc.id, dc.content, dc.content_type,
           d.external_id,
           d.metadata->>'title' AS title, d.metadata->>'date' AS date,
           d.source_uri, dc.metadata AS chunk_meta, dc.chunk_index,
           ROW_NUMBER() OVER (ORDER BY dc.embedding <=> $1::halfvec) AS rank_sem
    FROM document_chunks dc JOIN documents d ON dc.document_id = d.id
    WHERE d.content_type != 'org_upload'
    ORDER BY dc.embedding <=> $1::halfvec LIMIT $3
),
lexical AS (
    SELECT dc.id, dc.content, dc.content_type,
           d.external_id,
           d.metadata->>'title' AS title, d.metadata->>'date' AS date,
           d.source_uri, dc.metadata AS chunk_meta, dc.chunk_index,
           ROW_NUMBER() OVER (ORDER BY ts_rank_cd(dc.search_tsv, q) DESC) AS rank_lex
    FROM document_chunks dc JOIN documents d ON dc.document_id = d.id,
         plainto_tsquery('english', $2) q
    WHERE dc.search_tsv @@ q AND d.content_type != 'org_upload'
    ORDER BY ts_rank_cd(dc.search_tsv, q) DESC LIMIT $4
)
SELECT COALESCE(s.id, l.id) AS id,
       COALESCE(s.content, l.content) AS content,
       COALESCE(s.content_type, l.content_type) AS content_type,
       COALESCE(s.external_id, l.external_id) AS external_id,
       COALESCE(s.title, l.title) AS title,
       COALESCE(s.date, l.date) AS date,
       COALESCE(s.source_uri, l.source_uri) AS source_uri,
       COALESCE(s.chunk_meta, l.chunk_meta) AS chunk_meta,
       COALESCE(s.chunk_index, l.chunk_index) AS chunk_index,
       COALESCE(1.0 / (60 + s.rank_sem), 0) +
       COALESCE(1.0 / (60 + l.rank_lex), 0) AS rrf_score
FROM semantic s FULL OUTER JOIN lexical l ON s.id = l.id
ORDER BY rrf_score DESC LIMIT $5;
"""

# Namespace-scoped hybrid search.
_SQL_HYBRID_NS = """
WITH semantic AS (
    SELECT dc.id, dc.content, dc.content_type,
           d.external_id,
           d.metadata->>'title' AS title, d.metadata->>'date' AS date,
           d.source_uri, dc.metadata AS chunk_meta, dc.chunk_index,
           ROW_NUMBER() OVER (ORDER BY dc.embedding <=> $1::halfvec) AS rank_sem
    FROM document_chunks dc JOIN documents d ON dc.document_id = d.id
    WHERE d.namespace = ANY($2::text[])
      AND (d.expires_at IS NULL OR d.expires_at > NOW())
    ORDER BY dc.embedding <=> $1::halfvec LIMIT $4
),
lexical AS (
    SELECT dc.id, dc.content, dc.content_type,
           d.external_id,
           d.metadata->>'title' AS title, d.metadata->>'date' AS date,
           d.source_uri, dc.metadata AS chunk_meta, dc.chunk_index,
           ROW_NUMBER() OVER (ORDER BY ts_rank_cd(dc.search_tsv, q) DESC) AS rank_lex
    FROM document_chunks dc JOIN documents d ON dc.document_id = d.id,
         plainto_tsquery('english', $3) q
    WHERE dc.search_tsv @@ q AND d.namespace = ANY($2::text[])
      AND (d.expires_at IS NULL OR d.expires_at > NOW())
    ORDER BY ts_rank_cd(dc.search_tsv, q) DESC LIMIT $5
)
SELECT COALESCE(s.id, l.id) AS id,
       COALESCE(s.content, l.content) AS content,
       COALESCE(s.content_type, l.content_type) AS content_type,
       COALESCE(s.external_id, l.external_id) AS external_id,
       COALESCE(s.title, l.title) AS title,
       COALESCE(s.date, l.date) AS date,
       COALESCE(s.source_uri, l.source_uri) AS source_uri,
       COALESCE(s.chunk_meta, l.chunk_meta) AS chunk_meta,
       COALESCE(s.chunk_index, l.chunk_index) AS chunk_index,
       COALESCE(1.0 / (60 + s.rank_sem), 0) +
       COALESCE(1.0 / (60 + l.rank_lex), 0) AS rrf_score
FROM semantic s FULL OUTER JOIN lexical l ON s.id = l.id
ORDER BY rrf_score DESC LIMIT $6;
"""

_EMBEDDING_MODEL = "text-embedding-3-large"
_SEM_LIMIT = 30
_LEX_LIMIT = 30
_HYBRID_LIMIT = 25

_SQL_ORG_SEMANTIC = """
SELECT dc.id, dc.content, dc.content_type,
       d.external_id,
       d.metadata->>'title' AS title, d.metadata->>'date' AS date,
       d.source_uri, dc.metadata AS chunk_meta, dc.chunk_index,
       (dc.embedding <=> $1::halfvec) AS distance
FROM document_chunks dc JOIN documents d ON dc.document_id = d.id
WHERE d.namespace = $2
  AND (d.expires_at IS NULL OR d.expires_at > NOW())
ORDER BY dc.embedding <=> $1::halfvec
LIMIT 50;
"""
_ORG_SCORE_BOOST = 1.5

_CACHE_CHAT_ID = "global"

_MONTH_NAMES = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}


def _parse_date_from_query(query: str) -> Optional[str]:
    m = _DATE_IN_QUERY_PATTERN.search(query)
    if not m:
        return None
    try:
        if m.group(1):
            return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
        if m.group(4):
            month_name = m.group(4)[:3].lower()
            month = _MONTH_NAMES.get(month_name)
            if month is None:
                return None
            day, year = int(m.group(5)), int(m.group(6))
            return datetime(year, month, day).strftime("%Y-%m-%d")
        if m.group(7):
            month_name = m.group(7)[:3].lower()
            month = _MONTH_NAMES.get(month_name)
            if month is None:
                return None
            day, year = int(m.group(8)), int(m.group(9))
            return datetime(year, month, day).strftime("%Y-%m-%d")
    except (ValueError, IndexError):
        pass
    return None


def _doc_date_matches(doc_date: str, target_iso: str) -> bool:
    doc_date = (doc_date or "").strip()
    if not doc_date:
        return False
    if doc_date == target_iso or doc_date.startswith(target_iso):
        return True
    try:
        parsed = datetime.strptime(doc_date, "%B %d, %Y").strftime("%Y-%m-%d")
        return parsed == target_iso
    except (ValueError, TypeError):
        pass
    return False


def _rerank_by_date(results: list[dict[str, Any]], target_date: str) -> list[dict[str, Any]]:
    matching, rest = [], []
    for r in results:
        if _doc_date_matches(r.get("date", ""), target_date):
            matching.append(r)
        else:
            rest.append(r)
    matching.sort(key=lambda x: (x.get("chunk_index", 0), -(x.get("score", 0))))
    rest.sort(key=lambda x: -(x.get("score", 0)))
    return matching + rest


async def _embed(text: str) -> Optional[list[float]]:
    from openai import AsyncOpenAI
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        return None
    client = AsyncOpenAI(api_key=api_key)
    resp = await client.embeddings.create(model=_EMBEDDING_MODEL, input=[text])
    return resp.data[0].embedding


def _row_to_dict(row) -> dict[str, Any]:
    chunk_meta = row["chunk_meta"]
    if isinstance(chunk_meta, str):
        chunk_meta = json.loads(chunk_meta)
    elif chunk_meta is None:
        chunk_meta = {}
    return {
        "content": row["content"],
        "score": float(row["rrf_score"]),
        "content_type": row["content_type"],
        "external_id": row["external_id"] or "",
        "title": row["title"] or "",
        "date": row["date"] or "",
        "url": row["source_uri"] or "",
        "section": chunk_meta.get("section", ""),
        "chunk_index": row["chunk_index"],
    }


def _org_row_to_dict(row) -> dict[str, Any]:
    chunk_meta = row["chunk_meta"]
    if isinstance(chunk_meta, str):
        chunk_meta = json.loads(chunk_meta)
    elif chunk_meta is None:
        chunk_meta = {}
    distance = float(row["distance"]) if row["distance"] is not None else 1.0
    return {
        "content": row["content"],
        "score": max(0.0, 1.0 - distance),
        "content_type": row["content_type"],
        "external_id": row["external_id"] or "",
        "title": row["title"] or "",
        "date": row["date"] or "",
        "url": row["source_uri"] or "",
        "section": chunk_meta.get("section", ""),
        "chunk_index": row["chunk_index"],
    }


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------

@function_tool
async def search_knowledge_base(
    query: str,
    limit: int = 10,
) -> list[dict[str, Any]]:
    """Semantic search over Bridgeway's pgvector knowledge base.

    Args:
        query: Natural-language search query.
        limit: Max results to return (default 10).

    Returns:
        Ranked list of chunks with content, score, title, date, and URL.
    """
    import logging
    log = logging.getLogger(__name__)

    org_ns = _org_namespace.get()
    pgvector_ns = _pgvector_namespaces.get()

    params: dict = {"query": query, "limit": limit}
    cache_ns_key = f"ns:{','.join(sorted(pgvector_ns))}" if pgvector_ns else _CACHE_CHAT_ID
    if org_ns is None:
        cached = get_cached(cache_ns_key, "search_knowledge_base", params)
        if cached is not None:
            return cached

    pool = get_pg_pool()
    if pool is None:
        return [{"error": "pgvector database not available"}]

    embedding = await _embed(query)
    if embedding is None:
        return [{"error": "Embedding API not available"}]

    emb_str = "[" + ",".join(str(x) for x in embedding) + "]"

    if pgvector_ns:
        static_rows = await pool.fetch(
            _SQL_HYBRID_NS,
            emb_str, pgvector_ns, query, _SEM_LIMIT, _LEX_LIMIT, _HYBRID_LIMIT,
        )
        if org_ns is not None:
            org_rows = await pool.fetch(_SQL_ORG_SEMANTIC, emb_str, org_ns)
            global_rows = static_rows
            rows = None
        else:
            rows = static_rows
    elif org_ns is not None:
        global_rows = await pool.fetch(
            _SQL_HYBRID_ALL,
            emb_str, query, _SEM_LIMIT, _LEX_LIMIT, _HYBRID_LIMIT,
        )
        org_rows = await pool.fetch(_SQL_ORG_SEMANTIC, emb_str, org_ns)
        rows = None
    else:
        rows = await pool.fetch(
            _SQL_HYBRID_ALL,
            emb_str, query, _SEM_LIMIT, _LEX_LIMIT, _HYBRID_LIMIT,
        )

    if rows is None:
        global_candidates = [_row_to_dict(r) for r in global_rows]
        org_candidates = [_org_row_to_dict(r) for r in org_rows]
        candidates = global_candidates + org_candidates
    else:
        candidates = [_row_to_dict(r) for r in rows]

    from bubble.pgvector.reranker import rerank_chunks

    if org_ns is not None:
        all_scored = await rerank_chunks(query, candidates, top_n=len(candidates))
        for r in all_scored:
            if r.get("content_type") == "org_upload":
                r["rerank_score"] = r.get("rerank_score", 0.0) * _ORG_SCORE_BOOST
        all_scored.sort(key=lambda x: x.get("rerank_score", 0.0), reverse=True)
        result = all_scored[:limit]
    else:
        result = await rerank_chunks(query, candidates, top_n=limit)

    content_types = set(r.get("content_type") for r in result)
    if "newsreel" in content_types:
        parsed_date = _parse_date_from_query(query)
        if parsed_date:
            result = _rerank_by_date(result, parsed_date)

    if org_ns is None:
        set_cached(cache_ns_key, "search_knowledge_base", params, result)
    return result


_SQL_LIST_DOCUMENTS_NS = """
    SELECT content_type, metadata->>'title' AS title, metadata->>'date' AS date
    FROM documents
    WHERE namespace = ANY($1)
      AND (expires_at IS NULL OR expires_at > NOW())
    ORDER BY content_type, (metadata->>'date') DESC NULLS LAST
"""

_SQL_LIST_DOCUMENTS_GLOBAL = """
    SELECT content_type, metadata->>'title' AS title, metadata->>'date' AS date
    FROM documents
    WHERE content_type != 'org_upload'
      AND (expires_at IS NULL OR expires_at > NOW())
    ORDER BY content_type, (metadata->>'date') DESC NULLS LAST
"""


@function_tool
async def list_available_documents() -> str:
    """List documents available in this chat's knowledge base.

    Returns a summary grouped by content type with titles and dates.
    Call this before searching to understand what sources are available.

    Returns:
        Grouped summary of available documents as a JSON string.
    """
    pool = get_pg_pool()
    if pool is None:
        return json.dumps({"error": "pgvector database not available"})

    pgvector_ns = _pgvector_namespaces.get()
    org_ns = _org_namespace.get()

    all_namespaces: list[str] = list(pgvector_ns)
    if org_ns:
        all_namespaces.append(org_ns)

    if all_namespaces:
        rows = await pool.fetch(_SQL_LIST_DOCUMENTS_NS, all_namespaces)
    else:
        rows = await pool.fetch(_SQL_LIST_DOCUMENTS_GLOBAL)

    grouped: dict[str, list[dict[str, str | None]]] = {}
    for row in rows:
        ct = row["content_type"]
        grouped.setdefault(ct, []).append(
            {"title": row["title"], "date": row["date"]}
        )

    if not grouped:
        return json.dumps({"available_documents": {}, "total": 0})

    return json.dumps({
        "available_documents": grouped,
        "total": sum(len(v) for v in grouped.values()),
    })
