"""
Build calendar item alerts from newly detected resources.

When a new resource is detected and linked to one or more calendar items
(via "Related calendar items"), an alert is generated describing the change
(e.g. "new_agenda", "new_material") and attached to those calendar items.

This module is safe to use even when the Bubble Calendar Item type does not
yet have an "Alerts" field — the alerts are only written into the local
payload JSON, never POSTed to Bubble.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from datetime import datetime, timezone

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Alert type classification
# ---------------------------------------------------------------------------

_AGENDA_KEYWORDS = ("agenda",)
_MATERIAL_KEYWORDS = ("materials", "minutes", "slide", "presentation", "handout")
_MEETING_LINK_KEYWORDS = ("webex", "zoom", "teams", "call", "dial-in", "webcast")


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

    Each alert dict:
        type            – "new_agenda" | "new_material" | "new_meeting_link" | "new_resource"
        resource_name   – human-readable resource name
        resource_url    – URL of the detected resource
        detected_at     – ISO-8601 UTC timestamp

    Resources whose "Related calendar items" is empty are silently skipped.
    """
    alerts_by_cal_id: dict[str, list[dict]] = defaultdict(list)
    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    for i, res in enumerate(resources):
        related_cal_ids = res.get("Related calendar items") or []
        if not isinstance(related_cal_ids, list) or not related_cal_ids:
            continue

        ctx = resource_context[i] if resource_context and i < len(resource_context) else {}
        section_type = ctx.get("section_type", "")

        alert = {
            "type": classify_alert_type(res, section_type=section_type),
            "resource_name": (res.get("Name") or "Untitled").strip(),
            "resource_url": (res.get("URL") or "").strip(),
            "detected_at": now_iso,
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
    Return calendar items with alerts appended to their ``"Alerts"`` field.

    Matching uses either:
      - ``_id`` on existing Bubble calendar items (from snapshot/lookup), or
      - positional identity for newly-created calendar items in this run.

    Calendar items that have no matching alerts keep ``"Alerts": []`` (safe
    default even when the Bubble type has no Alerts field yet — the payload
    is only written to local JSON).

    Does NOT mutate the input list; returns shallow copies.
    """
    if not alerts_by_cal_id:
        return calendar_items

    updated: list[dict] = []
    matched_count = 0

    for cal in calendar_items:
        cal_copy = dict(cal)
        cal_id = cal_copy.get("_id")

        existing_alerts = cal_copy.get("Alerts")
        if not isinstance(existing_alerts, list):
            existing_alerts = []

        new_alerts = alerts_by_cal_id.get(cal_id, []) if cal_id else []
        if new_alerts:
            matched_count += 1

        cal_copy["Alerts"] = existing_alerts + new_alerts
        updated.append(cal_copy)

    if matched_count:
        log.info(
            "Attached alerts to %d existing calendar item(s) (by _id)",
            matched_count,
        )

    return updated
