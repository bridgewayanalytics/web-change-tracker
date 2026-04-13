"""
Write page chunks (for vectorization) to S3 as JSONL.

One file per target per run:
  page_chunks/<target_id>/YYYY/MM/DD/<run_id>.jsonl

Each line is one chunk: {"text": "...", "metadata": {...}}

Bucket: PAGE_CHUNK_BUCKET env var.
        Falls back to HTML_SNAPSHOT_BUCKET if unset.
        Skipped silently if neither is configured.
"""

import json
import logging
import os
from datetime import datetime, timezone

log = logging.getLogger(__name__)


def _get_bucket() -> str:
    return (
        os.environ.get("PAGE_CHUNK_BUCKET", "").strip()
        or os.environ.get("HTML_SNAPSHOT_BUCKET", "").strip()
    )


def store_page_chunks(
    chunks: list[dict],
    *,
    target_id: str,
    run_id: str,
    run_timestamp: int,
) -> str | None:
    """
    Write chunks as JSONL to S3.

    Returns S3 URI on success, None if skipped or failed. Never raises.
    """
    bucket = _get_bucket()
    if not bucket:
        return None
    if not chunks:
        return None

    try:
        dt = datetime.fromtimestamp(run_timestamp, tz=timezone.utc)
        key = (
            f"page_chunks/{target_id}"
            f"/{dt.year:04d}/{dt.month:02d}/{dt.day:02d}"
            f"/{run_id}.jsonl"
        )

        body = "\n".join(json.dumps(c, ensure_ascii=False) for c in chunks) + "\n"

        import boto3
        region = os.environ.get("AWS_REGION", "us-east-1")
        client = boto3.client("s3", region_name=region)
        client.put_object(
            Bucket=bucket,
            Key=key,
            Body=body.encode("utf-8"),
            ContentType="application/x-ndjson",
            Metadata={"target_id": target_id, "run_id": run_id, "chunk_count": str(len(chunks))},
        )

        uri = f"s3://{bucket}/{key}"
        log.info("Page chunks uploaded to %s (%d chunks)", uri, len(chunks))
        return uri

    except Exception as e:
        log.warning("Page chunk upload failed for %s: %s", target_id, e)
        return None
