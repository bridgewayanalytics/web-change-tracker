"""
Build calendar item alerts from newly detected resources.

When a new resource is detected and linked to one or more calendar items
(via "Related calendar items"), an alert is generated describing the change
(e.g. "Agenda Posted", "Materials Posted") and stored in AWS S3.

Alert payloads follow the Bubble "Alert" schema with additional fields:
    {
        "Alert type": "<label>",
        "date": "<ISO-8601 date>",
        "Related calendar item": "<bubble_calendar_item_id>",
        "Trigger URL": "<url_of_resource_that_triggered_alert>",
    }

Alerts are uploaded to S3 under the alerts/ prefix (same bucket as bubble reports).
"""

from __future__ import annotations

import logging
import os
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Alert type classification
# ---------------------------------------------------------------------------

_AGENDA_KEYWORDS = ("agenda",)
_MATERIAL_KEYWORDS = ("materials", "minutes", "slide", "presentation", "handout")
_MEETING_LINK_KEYWORDS = ("webex", "zoom", "teams", "call", "dial-in", "webcast")

# Human-readable alert type labels matching the Bubble Alert data type
ALERT_TYPE_LABELS: dict[str, str] = {
    "new_agenda": "Agenda Posted",
    "new_material": "Materials Posted",
    "new_meeting_link": "Meeting Link Posted",
    "new_resource": "New Resource",
}


def classify_alert_type(resource: dict, section_type: str = "") -> str:
    """
    Classify what kind of alert a newly-detected resource represents.

    Returns one of:
        "new_agenda", "new_material", "new_meeting_link", "new_resource"
    """
    text = " ".join([
        (resource.get("Name") or ""),
        (resource.get("URL") or ""),
        (resource.get("notes") or ""),
    ]).lower()

    if any(kw in text for kw in _AGENDA_KEYWORDS):
        return "new_agenda"
    if any(kw in text for kw in _MATERIAL_KEYWORDS):
        return "new_material"
    if any(kw in text for kw in _MEETING_LINK_KEYWORDS):
        return "new_meeting_link"

    # Fall back to section_type hint from the extractor
    if section_type in ("event_links", "events"):
        return "new_meeting_link"

    return "new_resource"


def _alert_type_label(alert_key: str) -> str:
    """Map internal alert key to the Bubble 'Alert type' display label."""
    return ALERT_TYPE_LABELS.get(alert_key, alert_key)


# ---------------------------------------------------------------------------
# Alert building
# ---------------------------------------------------------------------------


def build_calendar_alerts(
    resources: list[dict],
    resource_context: list[dict] | None = None,
) -> dict[str, list[dict]]:
    """
    Given enriched resources (with "Related calendar items" resolved to IDs),
    build a mapping of ``calendar_item_id → [alert, ...]``.

    Each alert dict matches the Bubble Alert data type:
        Alert type      – "Agenda Posted" | "Materials Posted" | "Meeting Link Posted" | "New Resource"
        date            – ISO-8601 date string (YYYY-MM-DD)

    Additional metadata fields (prefixed with ``__``) are included for
    debugging and are stripped before Bubble upload:
        __resource_name – human-readable resource name
        __resource_url  – URL of the detected resource
        __alert_key     – internal classification key (e.g. "new_agenda")

    Resources whose "Related calendar items" is empty are silently skipped.
    """
    alerts_by_cal_id: dict[str, list[dict]] = defaultdict(list)
    today_iso = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    for i, res in enumerate(resources):
        related_cal_ids = res.get("Related calendar items") or []
        if not isinstance(related_cal_ids, list) or not related_cal_ids:
            continue

        ctx = resource_context[i] if resource_context and i < len(resource_context) else {}
        section_type = ctx.get("section_type", "")

        alert_key = classify_alert_type(res, section_type=section_type)

        resource_url = (res.get("URL") or "").strip()

        alert = {
            "Alert type": _alert_type_label(alert_key),
            "date": today_iso,
            "Related calendar item": None,  # populated per cal_id below
            "Trigger URL": resource_url,
            # Debug metadata (stripped before upload by __ prefix convention)
            "__alert_key": alert_key,
            "__resource_name": (res.get("Name") or "Untitled").strip(),
            "__resource_url": resource_url,
        }

        for cal_id in related_cal_ids:
            if not cal_id:
                continue
            cal_alert = dict(alert)
            cal_alert["Related calendar item"] = cal_id
            alerts_by_cal_id[cal_id].append(cal_alert)

    total = sum(len(v) for v in alerts_by_cal_id.values())
    if total:
        log.info(
            "Calendar alerts: %d alert(s) across %d calendar item(s)",
            total,
            len(alerts_by_cal_id),
        )

    return dict(alerts_by_cal_id)


def attach_alerts_to_calendar_items(
    calendar_items: list[dict],
    alerts_by_cal_id: dict[str, list[dict]],
) -> list[dict]:
    """
    Return calendar items with alerts appended to their ``"alerts"`` field.

    Matching uses ``_id`` on existing Bubble calendar items (from snapshot/lookup).
    Calendar items without a matching ``_id`` keep ``"alerts": []``.

    Does NOT mutate the input list; returns shallow copies.
    """
    if not alerts_by_cal_id:
        return calendar_items

    updated: list[dict] = []
    matched_count = 0

    for cal in calendar_items:
        cal_copy = dict(cal)
        cal_id = cal_copy.get("_id")

        existing_alerts = cal_copy.get("alerts")
        if not isinstance(existing_alerts, list):
            existing_alerts = []

        new_alerts = alerts_by_cal_id.get(cal_id, []) if cal_id else []
        if new_alerts:
            matched_count += 1

        cal_copy["alerts"] = existing_alerts + new_alerts
        updated.append(cal_copy)

    if matched_count:
        log.info(
            "Attached alerts to %d existing calendar item(s) (by _id)",
            matched_count,
        )

    return updated


# ---------------------------------------------------------------------------
# S3 upload: store alert payloads in AWS
# ---------------------------------------------------------------------------

ALERTS_LOCAL_FILE = Path(__file__).resolve().parent.parent / "last_alerts.json"


def write_alerts_local(alerts_by_cal_id: dict[str, list[dict]]) -> Path:
    """
    Flatten alerts_by_cal_id into a single list and write to last_alerts.json.

    Returns the path to the written file.
    """
    import json

    all_alerts: list[dict] = []
    for alerts in alerts_by_cal_id.values():
        all_alerts.extend(alerts)

    ALERTS_LOCAL_FILE.write_text(
        json.dumps(all_alerts, indent=2, ensure_ascii=False), encoding="utf-8",
    )
    log.info("Wrote %d alert(s) to %s", len(all_alerts), ALERTS_LOCAL_FILE)
    return ALERTS_LOCAL_FILE


def upload_alerts_to_s3(
    alerts_by_cal_id: dict[str, list[dict]],
    run_timestamp: int,
) -> dict[str, Any]:
    """
    Write alert payloads to S3, mirroring the bubble report upload pattern.

    Uploads to:
      - s3://$BUBBLE_ARTIFACT_BUCKET/alerts/latest.json
      - s3://$BUBBLE_ARTIFACT_BUCKET/alerts/runs/YYYY/MM/DD/<run_id>.json

    Gated by ``BUBBLE_ARTIFACT_BUCKET`` env var. Logs warnings on failure, never raises.

    Returns a summary dict: ``{uploaded: int, errors: [str]}``.
    """
    summary: dict[str, Any] = {"uploaded": 0, "errors": []}

    if not alerts_by_cal_id:
        return summary

    # Write local file first
    write_alerts_local(alerts_by_cal_id)

    bucket = (os.environ.get("BUBBLE_ARTIFACT_BUCKET") or "").strip()
    if not bucket:
        log.debug("upload_alerts_to_s3 skipped: BUBBLE_ARTIFACT_BUCKET not set")
        return summary

    try:
        import boto3
    except Exception as e:
        log.warning("Alert S3 upload skipped: boto3 not available (%s)", e)
        return summary

    try:
        region = os.environ.get("AWS_REGION", "us-east-1")
        client = boto3.client("s3", region_name=region)

        dt = datetime.fromtimestamp(run_timestamp, tz=timezone.utc)
        run_id = (os.environ.get("RUN_ID") or "").strip() or f"run-{run_timestamp}"

        image_tag = (
            (os.environ.get("IMAGE_TAG") or "").strip()
            or (os.environ.get("GIT_SHA") or "").strip()
        )

        metadata: dict[str, str] = {"run_id": str(run_id)}
        if image_tag:
            metadata["image_tag"] = image_tag

        body = ALERTS_LOCAL_FILE.read_bytes()

        latest_key = "alerts/latest.json"
        versioned_key = (
            f"alerts/runs/{dt.year:04d}/{dt.month:02d}/{dt.day:02d}/{run_id}.json"
        )

        for key in (latest_key, versioned_key):
            try:
                client.put_object(
                    Bucket=bucket,
                    Key=key,
                    Body=body,
                    ContentType="application/json",
                    Metadata=metadata,
                )
                summary["uploaded"] += 1
                log.info("Uploaded alerts to s3://%s/%s", bucket, key)
            except Exception as e:
                msg = f"Alert upload failed for s3://{bucket}/{key}: {e}"
                log.warning(msg)
                summary["errors"].append(msg)

    except Exception as e:
        msg = f"Alert S3 upload encountered an unexpected error: {e}"
        log.warning(msg)
        summary["errors"].append(msg)

    log.info(
        "upload_alerts_to_s3 complete: uploaded=%d errors=%d",
        summary["uploaded"],
        len(summary["errors"]),
    )
    return summary
