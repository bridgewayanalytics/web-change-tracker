"""
Build calendar item alerts from newly detected resources.

When a new resource is detected and linked to one or more calendar items
(via "Related calendar items"), an alert is generated describing the change
(e.g. "Agenda Posted", "Materials Posted") and attached to those calendar items.

Alert payloads match the Bubble "Alert" data type:
    {"Alert type": "<label>", "date": "<ISO-8601 date>"}

Calendar items reference alerts via a list field:
    {"alerts": [<alert_id>, ...]}
"""

from __future__ import annotations

import logging
import os
from collections import defaultdict
from datetime import datetime, timezone
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

        alert = {
            "Alert type": _alert_type_label(alert_key),
            "date": today_iso,
            # Debug metadata (stripped before Bubble upload by __ prefix convention)
            "__alert_key": alert_key,
            "__resource_name": (res.get("Name") or "Untitled").strip(),
            "__resource_url": (res.get("URL") or "").strip(),
        }

        for cal_id in related_cal_ids:
            if not cal_id:
                continue
            alerts_by_cal_id[cal_id].append(alert)

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
# Bubble write: create Alert objects + patch Calendar Item.alerts
# ---------------------------------------------------------------------------


def flush_alerts_to_bubble(
    calendar_items: list[dict],
    alerts_by_cal_id: dict[str, list[dict]],
) -> dict[str, Any]:
    """
    Create Alert objects in Bubble and attach their IDs to Calendar Items.

    For each calendar item that has pending alerts:
      1. POST each alert to Bubble ``Alert`` type → get back ``_id``
      2. PATCH the Calendar Item's ``alerts`` list field to append the new IDs

    Gated by ``BUBBLE_ALERTS_ENABLED`` env var (must be ``1``/``true``/``yes``).
    This is independent of ``dry_run_bubble`` — alert writes are scoped by the
    client allowlist (only Alert create + Calendar Item patch_alerts permitted).

    Returns a summary dict: ``{created: int, patched: int, errors: [str]}``.
    Never raises — logs warnings on per-item failures and continues.
    """
    summary: dict[str, Any] = {"created": 0, "patched": 0, "errors": []}

    enabled = os.environ.get("BUBBLE_ALERTS_ENABLED", "").strip().lower() in ("1", "true", "yes")
    if not enabled:
        log.debug("flush_alerts_to_bubble skipped: BUBBLE_ALERTS_ENABLED not set")
        return summary

    if not alerts_by_cal_id:
        return summary

    from bubble.client import BubbleAPIError, get_client

    try:
        client = get_client()
    except Exception as e:
        msg = f"Cannot flush alerts: Bubble client init failed: {e}"
        log.warning(msg)
        summary["errors"].append(msg)
        return summary

    for cal_id, alerts in alerts_by_cal_id.items():
        if not cal_id or not alerts:
            continue

        # 1. Create each Alert object in Bubble
        new_alert_ids: list[str] = []
        for alert in alerts:
            bubble_fields = {
                "Alert type": alert["Alert type"],
                "date": alert["date"],
            }
            try:
                alert_id = client.create("Alert", bubble_fields)
                new_alert_ids.append(alert_id)
                summary["created"] += 1
                log.info(
                    "Created Bubble Alert: id=%s type=%r for calendar_item=%s",
                    alert_id,
                    alert["Alert type"],
                    cal_id,
                )
            except BubbleAPIError as e:
                msg = f"Failed to create Alert for calendar_item={cal_id}: {e}"
                log.warning(msg)
                summary["errors"].append(msg)

        if not new_alert_ids:
            continue

        # 2. Read existing alert IDs on the Calendar Item so we append, not overwrite
        existing_alert_ids: list[str] = []
        try:
            cal_obj = client.get("Calendar Item", cal_id)
            raw = cal_obj.get("alerts") or []
            if isinstance(raw, list):
                existing_alert_ids = [a if isinstance(a, str) else a.get("_id", "") for a in raw]
                existing_alert_ids = [a for a in existing_alert_ids if a]
        except BubbleAPIError as e:
            log.warning("Could not read existing alerts for calendar_item=%s: %s", cal_id, e)

        # 3. Patch the Calendar Item with the combined list
        merged_ids = existing_alert_ids + new_alert_ids
        try:
            client.patch(
                "Calendar Item",
                cal_id,
                {"alerts": merged_ids},
                scope="patch_alerts",
            )
            summary["patched"] += 1
            log.info(
                "Patched Calendar Item %s: alerts=%s",
                cal_id,
                merged_ids,
            )
        except BubbleAPIError as e:
            msg = f"Failed to patch alerts on calendar_item={cal_id}: {e}"
            log.warning(msg)
            summary["errors"].append(msg)

    log.info(
        "flush_alerts_to_bubble complete: created=%d patched=%d errors=%d",
        summary["created"],
        summary["patched"],
        len(summary["errors"]),
    )
    return summary
