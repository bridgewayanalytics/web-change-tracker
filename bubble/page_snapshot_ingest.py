"""
Client for ingesting before/after HTML page snapshots into pgvector so the
document extraction agent can retrieve them via search_knowledge_base instead
of carrying the raw HTML in its prompt.

Reuses the same `/knowledge/document-extraction/{doc_uuid}` ingest endpoint as
`bubble/doc_extraction_ingest.py` — one document per side (before, after),
each with its own deterministic uuid derived from the parent page-change
event's agent_call_id. Two separate namespaces per call so the agent can
query the two sides independently.

Deterministic uuid design:
  uuid = sha256(f"{agent_call_id}|{side}")[:32]

- Repeated extractions sharing the same parent agent_call_id (e.g. multiple
  library items from one page change, or dashboard reruns) hit the same
  namespace → no duplicate indexing, no wasted embed cost.
- Changing side (before vs after) produces a different uuid so each side is
  a separate searchable document.

Returns a list of namespace strings suitable for appending to the agent's
`pgvector_namespaces`. Empty list on any failure — the caller falls through
to the direct in-prompt HTML path.
"""
import hashlib
import logging

from bubble.doc_extraction_ingest import ingest_document, wait_until_ready

log = logging.getLogger(__name__)


def _snapshot_uuid(agent_call_id: str, side: str) -> str:
    """Deterministic uuid per (agent_call_id, side)."""
    return hashlib.sha256(f"{agent_call_id}|{side}".encode()).hexdigest()[:32]


def ingest_page_snapshots(
    agent_call_id: str,
    before_html: str,
    after_html: str,
) -> list[str]:
    """
    Upload the before+after HTML snapshots to pgvector as two separate documents.

    Returns a list of namespace strings (one per side successfully indexed)
    to append to the agent's pgvector_namespaces. Empty list on total failure.
    Partial success (e.g. before indexed, after failed) returns just the
    successful side.
    """
    if not agent_call_id:
        return []

    namespaces: list[str] = []
    for side, html in (("before", before_html), ("after", after_html)):
        if not html or not html.strip():
            continue
        doc_uuid = _snapshot_uuid(agent_call_id, side)
        filename = f"page-{side}-{agent_call_id[:12]}.html"
        try:
            resp = ingest_document(doc_uuid, filename, html.encode("utf-8"), "text/html")
            status = resp.get("status")
            log.info(
                "page_snapshot_ingest: side=%s status=%s doc_uuid=%s acid=%s",
                side, status, doc_uuid, agent_call_id[:12],
            )
            if status == "cached":
                namespaces.append(resp.get("namespace") or f"document-extraction:{doc_uuid}")
                continue
            # status == "queued" — poll until ready
            result = wait_until_ready(doc_uuid)
            namespaces.append(result.get("namespace") or f"document-extraction:{doc_uuid}")
            log.info(
                "page_snapshot_ingest: side=%s ready, chunks=%s",
                side, result.get("chunk_count"),
            )
        except Exception as e:
            log.warning(
                "page_snapshot_ingest: side=%s failed for acid=%s — proceeding without this side: %s",
                side, agent_call_id[:12], e,
            )
            continue

    return namespaces
