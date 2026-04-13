"""
Store pipeline alert output to S3 in a UI-ready structure.

Structure (all under CHANGELOG_BUCKET, falling back to BUBBLE_ARTIFACT_BUCKET):

  runs/YYYY/MM/DD/<run_id>/alerts.json
      Array of alert objects — one per changed page that produced agent output.
      This is the primary file for the Alerts Table dashboard UI.

  pages/<target_id>/YYYY/MM/DD/<run_id>/agent_output.json
      Full 20-field web tracking agent output for one page.

  pages/<target_id>/YYYY/MM/DD/<run_id>/doc_extractions.json
      Document agent results for library items found on that page.

Never raises — all failures are logged as warnings.
"""

import json
import logging
import os
from datetime import datetime, timezone

log = logging.getLogger(__name__)


def _get_bucket() -> str:
    return (
        os.environ.get("CHANGELOG_BUCKET", "").strip()
        or os.environ.get("BUBBLE_ARTIFACT_BUCKET", "").strip()
    )


def _s3_client():
    import boto3
    return boto3.client("s3", region_name=os.environ.get("AWS_REGION", "us-east-1"))


def _date_prefix(run_timestamp: int) -> str:
    dt = datetime.fromtimestamp(run_timestamp, tz=timezone.utc)
    return f"{dt.year:04d}/{dt.month:02d}/{dt.day:02d}"


def _put(client, bucket: str, key: str, obj: object, run_id: str) -> None:
    client.put_object(
        Bucket=bucket,
        Key=key,
        Body=json.dumps(obj, indent=2, ensure_ascii=False).encode("utf-8"),
        ContentType="application/json",
        Metadata={"run_id": run_id},
    )


def store_run_alerts(
    change_events: list[dict],
    run_id: str,
    run_timestamp: int,
) -> str | None:
    """
    Write runs/<date>/<run_id>/alerts.json and per-page agent_output/doc_extractions files.

    Only events with __agent_output and alert_type != "No Meaningful Change" are included
    in alerts.json. All processing is non-fatal.

    Returns the S3 URI of alerts.json on success, None if skipped or failed.
    """
    bucket = _get_bucket()
    if not bucket:
        return None

    date_prefix = _date_prefix(run_timestamp)
    run_timestamp_iso = datetime.fromtimestamp(run_timestamp, tz=timezone.utc).isoformat()

    alert_rows: list[dict] = []

    try:
        client = _s3_client()
    except Exception as e:
        log.warning("alert_s3: could not create S3 client: %s", e)
        return None

    for ev in change_events:
        if "error" in ev:
            continue
        agent_output = ev.get("__agent_output") or {}
        if not agent_output:
            continue
        if agent_output.get("alert_type") == "No Meaningful Change":
            continue

        target_id = ev.get("target_id") or "unknown"
        page_key_prefix = f"pages/{target_id}/{date_prefix}/{run_id}"

        # Write per-page agent_output.json
        agent_key = f"{page_key_prefix}/agent_output.json"
        try:
            _put(client, bucket, agent_key, agent_output, run_id)
        except Exception as e:
            log.warning("alert_s3: failed to write agent_output for %s: %s", target_id, e)
            agent_key = None

        # Write per-page doc_extractions.json (if present)
        doc_extractions = ev.get("__doc_extraction") or []
        doc_key = None
        if doc_extractions:
            doc_key = f"{page_key_prefix}/doc_extractions.json"
            try:
                _put(client, bucket, doc_key, doc_extractions, run_id)
            except Exception as e:
                log.warning("alert_s3: failed to write doc_extractions for %s: %s", target_id, e)
                doc_key = None

        # Build alert row for the run-level index
        row: dict = {
            "run_id": run_id,
            "run_timestamp": run_timestamp_iso,
            "target_id": target_id,
            "label": ev.get("label") or "",
            "url": ev.get("url") or "",
            "alert_type": agent_output.get("alert_type") or "",
            "alert_title": agent_output.get("alert_title") or "",
            "alert_description": agent_output.get("alert_description") or "",
            "alert_url": agent_output.get("alert_url"),
            "organization": agent_output.get("organization"),
            "alert_date_time": agent_output.get("alert_date_time"),
            "events": agent_output.get("events") or [],
            "library_items": agent_output.get("library_items") or [],
            "agenda_items": agent_output.get("agenda_items") or [],
            "doc_extractions": [e.get("extraction") or {} for e in doc_extractions],
        }
        if agent_key:
            row["detail_s3_key"] = agent_key
        alert_rows.append(row)

    # Always write alerts.json for the run (even if empty — signals run completed)
    alerts_key = f"runs/{date_prefix}/{run_id}/alerts.json"
    try:
        _put(client, bucket, alerts_key, alert_rows, run_id)
        uri = f"s3://{bucket}/{alerts_key}"
        log.info("alert_s3: wrote %d alert(s) to %s", len(alert_rows), uri)
        return uri
    except Exception as e:
        log.warning("alert_s3: failed to write alerts.json: %s", e)
        return None
