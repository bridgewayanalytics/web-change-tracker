"""
Fetch before/after HTML snapshots from S3 for a given alert row.

Snapshots are stored by page_change_s3.py at:
  pages/<target_id>/YYYY/MM/DD/<run_id>/before.html
  pages/<target_id>/YYYY/MM/DD/<run_id>/after.html
"""

import logging
import os
from datetime import datetime, timezone

log = logging.getLogger(__name__)

_DEFAULT_BUCKET = "web-change-tracker-prod-artifacts-815039343351"
_HTML_CHAR_LIMIT = 30_000


def _get_bucket() -> str:
    return (
        os.environ.get("PAGE_CHANGE_SNAPSHOT_BUCKET", "").strip()
        or os.environ.get("HTML_SNAPSHOT_BUCKET", "").strip()
        or os.environ.get("CHANGELOG_BUCKET", "").strip()
        or _DEFAULT_BUCKET
    )


def _s3_client():
    import boto3
    return boto3.client("s3", region_name=os.environ.get("AWS_REGION", "us-east-1"))


def _base_key(target_id: str, run_id: str, run_timestamp: int) -> str:
    dt = datetime.fromtimestamp(int(run_timestamp), tz=timezone.utc)
    return f"pages/{target_id}/{dt.year:04d}/{dt.month:02d}/{dt.day:02d}/{run_id}"


def fetch_html_snapshots(row: dict) -> tuple[str, str]:
    """
    Return (before_html, after_html) for a given alert row.
    Returns ("", "") if snapshots are not found or fetch fails.
    Truncates each to _HTML_CHAR_LIMIT characters to stay within token limits.
    """
    target_id = row.get("target_id", "")
    run_id = row.get("run_id", "")
    run_timestamp = row.get("run_timestamp")

    if not target_id or not run_id or not run_timestamp:
        log.warning("Row missing target_id/run_id/run_timestamp — cannot fetch HTML")
        return "", ""

    bucket = _get_bucket()
    client = _s3_client()
    base = _base_key(target_id, run_id, run_timestamp)

    def _get(key: str) -> str:
        try:
            resp = client.get_object(Bucket=bucket, Key=key)
            text = resp["Body"].read().decode("utf-8", errors="replace")
            return text[:_HTML_CHAR_LIMIT]
        except client.exceptions.NoSuchKey:
            log.debug("HTML snapshot not found: s3://%s/%s", bucket, key)
            return ""
        except Exception as e:
            log.warning("Failed to fetch %s: %s", key, e)
            return ""

    before = _get(f"{base}/before.html")
    after = _get(f"{base}/after.html")
    return before, after
