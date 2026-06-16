"""
Content creation ingest gate — approve, reject, and presigned URL utilities.

These functions are called by the dashboard API routes (NAICDashboard-) to
approve or reject rows in alerts_table.jsonl and document_extractions_table.jsonl
for ingest into the newsreel-generation knowledge base.

ingest_status lifecycle:
  null           — row not eligible for ingest
  "pending"      — ready for review (set by spike.py after chunking or doc relevance check)
  "approved"     — sent to knowledge base (set here after successful ingest call)
  "rejected"     — dismissed, will not be ingested (set here on reject)
"""

import logging
import os

log = logging.getLogger(__name__)

_ALERTS_TABLE_KEY = "alerts/alerts_table.jsonl"
_DOC_TABLE_KEY = "alerts/document_extractions_table.jsonl"


def _get_bucket() -> str:
    from storage.alert_s3 import _get_bucket as _base_get_bucket
    return _base_get_bucket()


def _s3_client():
    import boto3
    return boto3.client("s3", region_name=os.environ.get("AWS_REGION", "us-east-1"))


def _find_row(jsonl_key: str, match_fields: dict, bucket: str) -> dict | None:
    try:
        import json
        client = _s3_client()
        body = client.get_object(Bucket=bucket, Key=jsonl_key)["Body"].read().decode("utf-8")
        for line in body.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
                if all(row.get(k) == v for k, v in match_fields.items()):
                    return row
            except Exception:
                pass
    except Exception as exc:
        log.warning("ingest_actions: could not read %s: %s", jsonl_key, exc)
    return None


def approve_transcript_ingest(agent_call_id: str) -> bool:
    """
    Approve a transcript for ingest: generates a presigned URL for the transcript
    txt file and submits it to the newsreel-generation knowledge base via
    ingest_for_newsreel(), then patches the alert row to ingest_status: "approved".
    """
    bucket = _get_bucket()
    row = _find_row(_ALERTS_TABLE_KEY, {"agent_call_id": agent_call_id}, bucket)
    if not row:
        log.warning("approve_transcript_ingest: no row for agent_call_id=%s", agent_call_id)
        return False

    transcript_key = row.get("transcript_s3_key") or row.get("manual_transcript_s3_key")
    if not transcript_key:
        log.warning("approve_transcript_ingest: no transcript_s3_key for agent_call_id=%s", agent_call_id)
        return False

    presigned_url = generate_presigned_url(transcript_key, expires_in=3600)
    filename = transcript_key.split("/")[-1] or "transcript.txt"

    from bubble.newsreel_ingest import ingest_for_newsreel
    ingest_for_newsreel(document_url=presigned_url, filename=filename)

    from storage.alert_s3 import patch_jsonl_row
    patch_jsonl_row(
        _ALERTS_TABLE_KEY,
        {"agent_call_id": agent_call_id},
        {"ingest_status": "approved"},
        bucket=bucket,
    )
    return True


def approve_document_ingest(agent_call_id: str, library_item_url: str) -> bool:
    """
    Approve a document for ingest: calls ingest-document API with the library item URL,
    then patches the doc extraction row to ingest_status: "approved".

    Matches on (agent_call_id, library_item_url) to target a specific document row.
    """
    bucket = _get_bucket()
    row = _find_row(
        _DOC_TABLE_KEY,
        {"agent_call_id": agent_call_id, "library_item_url": library_item_url},
        bucket,
    )
    if not row:
        log.warning(
            "approve_document_ingest: no row for agent_call_id=%s url=%s",
            agent_call_id, library_item_url,
        )
        return False

    url = row.get("library_item_url") or ""
    name = row.get("library_item_title") or ""
    if not url:
        log.warning("approve_document_ingest: no library_item_url on row agent_call_id=%s", agent_call_id)
        return False

    from bubble.newsreel_ingest import ingest_for_newsreel
    ingest_for_newsreel(document_url=url, filename=name)

    from storage.alert_s3 import patch_jsonl_row
    patch_jsonl_row(
        _DOC_TABLE_KEY,
        {"agent_call_id": agent_call_id, "library_item_url": library_item_url},
        {"ingest_status": "approved"},
        bucket=bucket,
    )
    return True


def ingest_manual_document_url(url: str, filename: str) -> bool:
    """
    Ingest a manually provided document URL into the newsreel knowledge base.
    Does not create a row — for ad-hoc document ingest from the dashboard.
    """
    if not url:
        log.warning("ingest_manual_document_url: no URL provided")
        return False
    from bubble.newsreel_ingest import ingest_for_newsreel
    ingest_for_newsreel(document_url=url, filename=filename)
    return True


def reject_ingest(table: str, match_fields: dict) -> bool:
    """
    Reject a row: patches ingest_status to "rejected".

    table: "alerts" or "docs"
    match_fields: dict of fields to match on, e.g. {"agent_call_id": "abc123"}
      For doc extraction rows, include "library_item_url" to target a specific document.
    """
    jsonl_key = _ALERTS_TABLE_KEY if table == "alerts" else _DOC_TABLE_KEY
    bucket = _get_bucket()
    from storage.alert_s3 import patch_jsonl_row
    patched = patch_jsonl_row(jsonl_key, match_fields, {"ingest_status": "rejected"}, bucket=bucket)
    return patched > 0


def generate_presigned_url(s3_key: str, expires_in: int = 3600) -> str:
    """
    Generate a presigned S3 GET URL for a recording or transcript file.
    Default expiry: 1 hour.
    """
    bucket = _get_bucket()
    client = _s3_client()
    return client.generate_presigned_url(
        "get_object",
        Params={"Bucket": bucket, "Key": s3_key},
        ExpiresIn=expires_in,
    )


def generate_presigned_upload_url(s3_key: str, content_type: str = "text/plain", expires_in: int = 900) -> str:
    """
    Generate a presigned S3 PUT URL for uploading a manual transcript file.
    Default expiry: 15 minutes. The dashboard browser uploads directly to S3,
    then triggers the MANUAL_CHUNK ECS task with the resulting s3_key.
    """
    bucket = _get_bucket()
    client = _s3_client()
    return client.generate_presigned_url(
        "put_object",
        Params={"Bucket": bucket, "Key": s3_key, "ContentType": content_type},
        ExpiresIn=expires_in,
    )
