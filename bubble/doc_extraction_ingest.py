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
_DEFAULT_TIMEOUT_S = 300  # 5 min — enough for CPU worker; GPU cold-start can take longer
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
) -> dict:
    """Upload a document for indexing. Returns the API response dict."""
    r = requests.post(
        f"{_CHAT_API_BASE}/knowledge/document-extraction/{doc_uuid}",
        headers=_headers(),
        files={"file": (filename, content, mime_type)},
        data={"force": "true"} if force else {},
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
    Falls back to None gracefully so the agent still runs without the namespace.
    """
    if not pdf_text or not pdf_text.strip():
        log.debug("doc_extraction_ingest: no text for %s — skipping ingest", document_name[:60])
        return None

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
