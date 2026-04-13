"""
Store before/after stripped HTML snapshots to S3 when a page change is detected.

Uploads three files per changed target per run:
  page_change_diffs/<target_id>/YYYY/MM/DD/<run_id>/before.html
  page_change_diffs/<target_id>/YYYY/MM/DD/<run_id>/after.html
  page_change_diffs/<target_id>/YYYY/MM/DD/<run_id>/meta.json

Bucket: PAGE_CHANGE_SNAPSHOT_BUCKET env var.
        Falls back to HTML_SNAPSHOT_BUCKET if unset.
        Skipped silently if neither is configured.
"""

import hashlib
import json
import logging
import os
from datetime import datetime, timezone

log = logging.getLogger(__name__)

_PREFIX = "pages"


def _get_bucket() -> str:
    return (
        os.environ.get("PAGE_CHANGE_SNAPSHOT_BUCKET", "").strip()
        or os.environ.get("HTML_SNAPSHOT_BUCKET", "").strip()
    )


def _s3_client():
    import boto3
    region = os.environ.get("AWS_REGION", "us-east-1")
    return boto3.client("s3", region_name=region)


def store_page_change(
    *,
    target_id: str,
    run_id: str,
    run_timestamp: int,
    label: str,
    url: str,
    before_html: str,
    after_html: str,
    before_hash: str | None = None,
    after_hash: str | None = None,
    first_run: bool = False,
) -> str | None:
    """
    Upload before.html, after.html, and meta.json to S3 for one changed target.

    Returns the S3 URI prefix (without filename) on success, None if skipped or failed.
    Never raises.
    """
    bucket = _get_bucket()
    if not bucket:
        return None

    try:
        client = _s3_client()
        dt = datetime.fromtimestamp(run_timestamp, tz=timezone.utc)
        base_key = (
            f"{_PREFIX}/{target_id}"
            f"/{dt.year:04d}/{dt.month:02d}/{dt.day:02d}"
            f"/{run_id}"
        )

        before_bytes = before_html.encode("utf-8") if before_html else b""
        after_bytes = after_html.encode("utf-8")

        computed_before_hash = (
            before_hash
            or (hashlib.sha256(before_bytes).hexdigest() if before_bytes else "")
        )
        computed_after_hash = after_hash or hashlib.sha256(after_bytes).hexdigest()

        common_meta = {
            "run_id": run_id,
            "target_id": target_id,
        }

        # before.html
        client.put_object(
            Bucket=bucket,
            Key=f"{base_key}/before.html",
            Body=before_bytes,
            ContentType="text/html; charset=utf-8",
            Metadata=common_meta,
        )

        # after.html
        client.put_object(
            Bucket=bucket,
            Key=f"{base_key}/after.html",
            Body=after_bytes,
            ContentType="text/html; charset=utf-8",
            Metadata=common_meta,
        )

        # meta.json
        meta = {
            "run_id": run_id,
            "run_timestamp": run_timestamp,
            "target_id": target_id,
            "label": label,
            "url": url,
            "first_run": first_run,
            "before_size_bytes": len(before_bytes),
            "after_size_bytes": len(after_bytes),
            "before_hash": computed_before_hash,
            "after_hash": computed_after_hash,
        }
        client.put_object(
            Bucket=bucket,
            Key=f"{base_key}/meta.json",
            Body=json.dumps(meta, indent=2).encode("utf-8"),
            ContentType="application/json",
            Metadata=common_meta,
        )

        uri = f"s3://{bucket}/{base_key}/"
        log.info("Page change snapshots uploaded to %s", uri)
        return uri

    except Exception as e:
        log.warning("Page change snapshot upload failed for %s: %s", target_id, e)
        return None
