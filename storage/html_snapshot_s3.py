"""Upload raw HTML snapshots to S3 for archival. Bucket via HTML_SNAPSHOT_BUCKET."""

import hashlib
import logging
import os
from datetime import datetime, timezone
from urllib.parse import urlparse

log = logging.getLogger(__name__)

# Per-run dedup: set of (url, content_hash) already uploaded this process.
_uploaded: set[tuple[str, str]] = set()


def store_html_snapshot(
    html: str,
    url: str,
    run_id: str,
    run_timestamp: int,
) -> str | None:
    """
    Upload raw HTML to S3 if HTML_SNAPSHOT_BUCKET is set.

    Path: html_snapshots/<domain>/YYYY/MM/DD/<run_id>.html
    Returns the S3 URI on success, None if skipped, never raises.
    """
    bucket = (os.environ.get("HTML_SNAPSHOT_BUCKET") or "").strip()
    if not bucket:
        return None

    try:
        content_hash = hashlib.sha256(html.encode("utf-8")).hexdigest()

        # Dedup: skip if same URL + hash already uploaded in this run.
        dedup_key = (url, content_hash)
        if dedup_key in _uploaded:
            log.debug("HTML snapshot skipped (duplicate): %s", url)
            return None

        domain = urlparse(url).netloc or "unknown"
        dt = datetime.fromtimestamp(run_timestamp, tz=timezone.utc)
        key = (
            f"html_snapshots/{domain}"
            f"/{dt.year:04d}/{dt.month:02d}/{dt.day:02d}"
            f"/{run_id}.html"
        )

        # Git SHA: same env-var convention as _upload_bubble_report_to_s3.
        git_sha = (
            (os.environ.get("IMAGE_TAG") or "").strip()
            or (os.environ.get("GIT_SHA") or "").strip()
        )

        metadata: dict[str, str] = {
            "run_id": run_id,
            "source_url": url,
            "timestamp": str(run_timestamp),
            "content_hash": content_hash,
        }
        if git_sha:
            metadata["git_sha"] = git_sha

        import boto3

        region = os.environ.get("AWS_REGION", "us-east-1")
        client = boto3.client("s3", region_name=region)
        client.put_object(
            Bucket=bucket,
            Key=key,
            Body=html.encode("utf-8"),
            ContentType="text/html; charset=utf-8",
            Metadata=metadata,
        )

        _uploaded.add(dedup_key)
        log.info("HTML snapshot uploaded to s3://%s/%s", bucket, key)
        return f"s3://{bucket}/{key}"

    except Exception as e:
        log.warning("HTML snapshot upload failed for %s: %s", url, e)
        return None
