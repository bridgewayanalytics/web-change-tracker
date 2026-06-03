"""
Push documents and transcript chunks into the art-newsreel-generation
knowledge base via the ChatKit internal ingest API.

- ingest_for_newsreel(): submits a raw document URL (PDF, report, agenda)
  via POST /internal/documents/ingest. Async — backend queues a GPU task.
- ingest_transcript_chunks(): submits a transcript JSONL already in S3
  via POST /internal/documents/ingest-transcript-chunks. Synchronous.

Never raises — all failures are logged at WARNING for CloudWatch audit.
"""

import logging
import os
from urllib.parse import urlparse

import requests

log = logging.getLogger(__name__)

_NAMESPACE = "newsreel-generation:ART"


def _base_url() -> str:
    return os.environ.get("CHATKIT_API_URL", "https://chat-api.bridgewayanalytics.com").rstrip("/")


def _ingest_api_url() -> str:
    return os.environ.get("INGEST_API_URL", "https://api.bridgewayanalytics.com").rstrip("/")


def _api_key() -> str:
    return os.environ.get("CHATKIT_INTERNAL_API_KEY", "")


def _url_basename(url: str) -> str:
    try:
        return urlparse(url).path.rstrip("/").split("/")[-1] or url
    except Exception:
        return url


def ingest_for_newsreel(document_url: str, filename: str) -> None:
    """
    Submit a document URL to the newsreel-generation knowledge base.

    Uses content-hash deduplication on the backend — safe to call multiple
    times for the same document without creating duplicates.
    """
    api_key = _api_key()
    if not api_key:
        log.warning(
            "newsreel_ingest: CHATKIT_INTERNAL_API_KEY not configured — "
            "skipping ingest of '%s'", filename[:80]
        )
        return

    if not document_url:
        log.warning(
            "newsreel_ingest: no URL provided for '%s' — skipping", filename[:80]
        )
        return

    effective_filename = filename.strip() or _url_basename(document_url)
    endpoint = f"{_base_url()}/internal/documents/ingest"

    try:
        resp = requests.post(
            endpoint,
            headers={"x-api-key": api_key},
            data={
                "namespace": _NAMESPACE,
                "filename": effective_filename,
                "url": document_url,
            },
            timeout=30,
        )

        if resp.status_code in (200, 201, 202):
            try:
                doc_id = resp.json().get("document_id", "?")
            except Exception:
                doc_id = "?"
            log.info(
                "newsreel_ingest: submitted '%s' → status=%s document_id=%s url=%s",
                effective_filename[:80], resp.status_code, doc_id, document_url[:120],
            )
        else:
            log.warning(
                "newsreel_ingest: FAILED for '%s' → HTTP %s: %s | url=%s",
                effective_filename[:80], resp.status_code,
                resp.text[:300], document_url[:120],
            )

    except requests.Timeout:
        log.warning(
            "newsreel_ingest: TIMEOUT after 30s for '%s' (%s)",
            effective_filename[:80], document_url[:120],
        )
    except Exception as exc:
        log.warning(
            "newsreel_ingest: ERROR for '%s' (%s): %s",
            effective_filename[:80], document_url[:120], exc,
        )


def ingest_transcript_chunks(s3_bucket: str, s3_key: str) -> None:
    """
    Submit a transcript JSONL file in S3 to the newsreel-generation knowledge base.

    The backend reads the file directly from S3 using its IAM role, embeds all
    chunk text fields, and writes to pgvector. Synchronous — returns only after
    indexing is complete (~5–15s). Safe to call multiple times for the same file.
    """
    api_key = _api_key()
    if not api_key:
        log.warning(
            "newsreel_ingest: CHATKIT_INTERNAL_API_KEY not configured — "
            "skipping transcript chunk ingest for s3://%s/%s", s3_bucket, s3_key
        )
        return

    endpoint = f"{_ingest_api_url()}/internal/documents/ingest-transcript-chunks"

    try:
        resp = requests.post(
            endpoint,
            headers={"x-api-key": api_key, "Content-Type": "application/json"},
            json={
                "s3_bucket": s3_bucket,
                "s3_key": s3_key,
                "namespace": _NAMESPACE,
            },
            timeout=60,
        )

        if resp.status_code in (200, 201):
            body = resp.json()
            status = body.get("status", "?")
            count = body.get("chunk_count", "?")
            log.info(
                "newsreel_ingest: transcript chunks → status=%s chunks=%s key=%s",
                status, count, s3_key,
            )
        else:
            log.warning(
                "newsreel_ingest: transcript chunk ingest FAILED → HTTP %s: %s | key=%s",
                resp.status_code, resp.text[:300], s3_key,
            )

    except requests.Timeout:
        log.warning(
            "newsreel_ingest: TIMEOUT after 60s for transcript chunk ingest key=%s", s3_key,
        )
    except Exception as exc:
        log.warning(
            "newsreel_ingest: ERROR ingesting transcript chunks key=%s: %s", s3_key, exc,
        )
