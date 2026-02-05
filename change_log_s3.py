"""Append change events as JSON lines to S3. Bucket via env CHANGELOG_BUCKET."""

import json
import os
from datetime import datetime, timezone
from typing import Any


def _serialize_event(e: dict[str, Any]) -> dict[str, Any]:
    """Produce a JSON-serializable event for changelog (strip non-serializable)."""
    out: dict[str, Any] = {}
    for k, v in e.items():
        if k == "change" and isinstance(v, dict):
            out[k] = v
        elif k in ("target_id", "label", "url", "error"):
            out[k] = v
        elif isinstance(v, (str, int, float, bool, type(None))):
            out[k] = v
        elif isinstance(v, dict):
            out[k] = _serialize_event(v)
        elif isinstance(v, list):
            out[k] = [_serialize_event(x) if isinstance(x, dict) else x for x in v]
        else:
            out[k] = str(v)
    return out


def append_change_events(change_events: list[dict[str, Any]]) -> str | None:
    """
    Write change events (with changes or errors) as JSON lines to S3.
    Path: changelog/YYYY/MM/DD/run-<timestamp>.jsonl
    Returns the S3 URI if written, None if CHANGELOG_BUCKET not set or no events.
    """
    bucket = os.environ.get("CHANGELOG_BUCKET", "").strip()
    if not bucket:
        return None

    events_to_log = [
        e
        for e in change_events
        if "error" in e or ("change" in e and _has_changes(e["change"]))
    ]
    if not events_to_log:
        return None

    region = os.environ.get("AWS_REGION", "us-east-1")
    now = datetime.now(timezone.utc)
    key = f"changelog/{now.year:04d}/{now.month:02d}/{now.day:02d}/run-{int(now.timestamp())}.jsonl"
    lines = [json.dumps(_serialize_event(e)) + "\n" for e in events_to_log]
    body = "".join(lines)

    import boto3

    client = boto3.client("s3", region_name=region)
    client.put_object(Bucket=bucket, Key=key, Body=body.encode("utf-8"), ContentType="application/x-ndjson")
    uri = f"s3://{bucket}/{key}"
    return uri


def _has_changes(ch: dict) -> bool:
    return ch.get("first_run") or ch.get("page_changed") or bool(ch.get("by_type"))
