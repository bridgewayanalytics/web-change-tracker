"""
Client for the chat backend's document-extraction ingestion API.

Uploads a document (as text/plain from already-extracted text, or raw PDF bytes)
to the chat-api pgvector store so the document extraction agent can search the
full document — not just the first 12,000 characters passed in the prompt.

The doc_uuid is deterministic per source URL, so:
  - Re-uploads of the same document are no-ops (content-hash dedup → "cached")
  - The QA agent re-derives the same uuid and searches the same indexed content
  - Reruns of the same alert hit the same namespace without re-indexing

Environment variables:
  CHAT_API_BASE          Base URL for the chat API (default: https://chat-api.bridgewayanalytics.com)
  CHATKIT_INTERNAL_API_KEY  Shared API key (required when this feature is used)
"""

import hashlib
import logging
import os
import time

import requests

log = logging.getLogger(__name__)

_CHAT_API_BASE = os.environ.get("CHAT_API_BASE", "https://chat-api.bridgewayanalytics.com")
_DEFAULT_TIMEOUT_S = 600  # 10 min — large meeting packets can take 6-8 min to chunk
_POLL_INTERVAL_S = 10


def _headers() -> dict:
    key = os.environ.get("CHATKIT_INTERNAL_API_KEY", "").strip()
    if not key:
        raise ValueError("CHATKIT_INTERNAL_API_KEY env var must be set to use document vectorisation")
    return {"x-api-key": key}


def doc_uuid_for(library_item_url: str) -> str:
    """Deterministic doc_uuid derived from the document URL (SHA256[:32])."""
    return hashlib.sha256(library_item_url.strip().encode()).hexdigest()[:32]


def ingest_document(
    doc_uuid: str,
    filename: str,
    content: bytes,
    mime_type: str,
    force: bool = False,
    ttl_hours: int | None = None,
) -> dict:
    """Upload a document for indexing. Returns the API response dict.

    ttl_hours: optional 1-8784 hour TTL after which the document and its chunks
    are purged server-side. Use for per-pipeline-run ingests so abandoned
    namespaces don't accumulate. Omit to persist until DELETE.
    """
    data: dict = {}
    if force:
        data["force"] = "true"
    if ttl_hours is not None:
        data["ttl_hours"] = str(int(ttl_hours))
    r = requests.post(
        f"{_CHAT_API_BASE}/knowledge/document-extraction/{doc_uuid}",
        headers=_headers(),
        files={"file": (filename, content, mime_type)},
        data=data,
        timeout=120,
    )
    r.raise_for_status()
    return r.json()


def get_status(doc_uuid: str) -> dict:
    """Return the current indexing status dict for a doc_uuid. 404 → returns {}."""
    r = requests.get(
        f"{_CHAT_API_BASE}/knowledge/document-extraction/{doc_uuid}/status",
        headers=_headers(),
        timeout=30,
    )
    if r.status_code == 404:
        return {}
    r.raise_for_status()
    return r.json()


def wait_until_ready(doc_uuid: str, timeout_s: int = _DEFAULT_TIMEOUT_S) -> dict:
    """Poll until the document is searchable. Raises RuntimeError on failure/timeout."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        body = get_status(doc_uuid)
        status = body.get("status")
        if status == "ready":
            return body
        if status == "failed":
            raise RuntimeError(f"document indexing failed for {doc_uuid}: {body.get('error')}")
        if body.get("gpu_note"):
            log.info("doc_extraction_ingest: GPU worker cold-starting for %s", doc_uuid)
        time.sleep(_POLL_INTERVAL_S)
    raise TimeoutError(f"document {doc_uuid} not ready after {timeout_s}s")


def ingest_and_arm(
    document_url: str,
    pdf_text: str | None,
    document_name: str,
) -> str | None:
    """
    Upload the document text to pgvector and wait until searchable.

    Returns the namespace string ("document-extraction:{doc_uuid}") to append
    to the agent's namespace list, or None if ingestion was skipped or failed.

    Uses text/plain (the already-extracted pdf_text) for fast indexing —
    seconds on the CPU worker vs 3–10 min for a raw PDF on the GPU worker.

    If pdf_text is unavailable (PDF fetch failed), checks whether the document
    was already indexed in a previous run and returns the cached namespace if so.
    Falls back to None gracefully so the agent still runs without the namespace.
    """
    if not pdf_text or not pdf_text.strip():
        log.info(
            "doc_extraction_ingest: no text for %s — checking if already indexed (pdf_text unavailable)",
            document_name[:60],
        )
        return arm_if_ready(document_url)

    if not document_url or not document_url.strip():
        return None

    try:
        doc_uuid = doc_uuid_for(document_url)
        filename = f"{doc_uuid}.txt"

        resp = ingest_document(doc_uuid, filename, pdf_text.encode("utf-8"), "text/plain")
        status = resp.get("status")
        log.info(
            "doc_extraction_ingest: ingest status=%s doc_uuid=%s document=%s",
            status, doc_uuid, document_name[:60],
        )

        if status == "cached":
            # Already indexed — no need to poll
            namespace = resp.get("namespace") or f"document-extraction:{doc_uuid}"
            log.info("doc_extraction_ingest: cached, namespace=%s", namespace)
            return namespace

        # status == "queued" — poll until ready
        result = wait_until_ready(doc_uuid)
        namespace = result.get("namespace") or f"document-extraction:{doc_uuid}"
        log.info(
            "doc_extraction_ingest: ready, namespace=%s chunks=%s",
            namespace, result.get("chunk_count"),
        )
        return namespace

    except Exception as e:
        log.warning("doc_extraction_ingest: failed for %s — proceeding without namespace: %s", document_name[:60], e)
        return None


def arm_if_ready(library_item_url: str) -> str | None:
    """
    Check if a document is already indexed (e.g. for QA agent runs).
    Returns the namespace if status is 'ready', None otherwise.
    Does NOT upload anything — use ingest_and_arm() to upload.
    """
    if not library_item_url or not library_item_url.strip():
        return None
    try:
        doc_uuid = doc_uuid_for(library_item_url)
        body = get_status(doc_uuid)
        if body.get("status") == "ready":
            namespace = body.get("namespace") or f"document-extraction:{doc_uuid}"
            log.info("doc_extraction_ingest: document already indexed, namespace=%s", namespace)
            return namespace
        log.debug("doc_extraction_ingest: document not ready (status=%s) for %s", body.get("status"), doc_uuid)
        return None
    except Exception as e:
        log.debug("doc_extraction_ingest: status check failed for %s: %s", library_item_url[:80], e)
        return None


def document_text(doc_uuid: str, timeout_s: int = 60) -> dict:
    """Fetch the whole indexed document as one string, in reading order.

    For text/plain ingests the backend removes the sliding-window overlaps
    at each seam. For PDFs it inserts `--- Page N ---` markers. Use this to
    inline the source document into an agent prompt rather than searching
    for it after the fact.

    Returns the API response dict with at least:
      document_id (int), title (str), page_count (int), chunk_count (int),
      char_count (int), text (str), overlaps_removed (int, text/plain only)

    Raises requests.HTTPError on 4xx/5xx. 404 means the document is not
    indexed (yet); call wait_until_ready() first.
    """
    r = requests.get(
        f"{_CHAT_API_BASE}/knowledge/document-extraction/{doc_uuid}/text",
        headers=_headers(),
        timeout=timeout_s,
    )
    r.raise_for_status()
    return r.json()


def search(
    query: str,
    namespaces: list[str],
    *,
    document_extraction_id: str | None = None,
    document_id: int | None = None,
    limit: int = 10,
    trace_id: str | None = None,
    timeout_s: int = 30,
) -> dict:
    """Server-side hybrid search + Decisions-API reranker.

    namespaces is the list of reference collections to search (e.g.
    ["ba:chronicles", "ba:newsreels", "ba:NAIC Guidelines", "naic-proceedings"]).
    Do NOT pass "document-extraction:*" namespaces — pass document_extraction_id
    instead and the server constructs the namespace string exactly as ingest does.

    document_id scopes search within a single document found in a prior call.

    trace_id is written to retrieval_traces.thread_id so every search in one
    pipeline run reads back together in the admin dashboard — pass the pipeline
    run_id.

    Returns:
      { "query": str, "namespaces": [str], "document_id": int | None,
        "trace_id": str, "count": int, "results": [ ... ] }
    Each result dict has: document_id, chunk_id, chunk_index, title, date, url,
    content_type, page_no, page_end, section_path, section_pages, score, content.
    """
    body: dict = {
        "query": query,
        "namespaces": list(namespaces),
        "limit": max(1, min(int(limit), 25)),
    }
    if document_extraction_id:
        body["document_extraction_id"] = document_extraction_id
    if document_id is not None:
        body["document_id"] = int(document_id)
    if trace_id:
        body["trace_id"] = trace_id
    r = requests.post(
        f"{_CHAT_API_BASE}/knowledge/search",
        headers=_headers(),
        json=body,
        timeout=timeout_s,
    )
    r.raise_for_status()
    return r.json()
