"""
Build Bubble Resource create preview payload from change events.

Maps added items (docs, event_links, events, meetings) to the Bubble Resource schema.
"""

from bubble_resources import BUBBLE_RESOURCE_FIELDS


def _item_should_hide(item: dict, rtype: str) -> bool:
    """True if item should be excluded from Bubble payload (denied URLs)."""
    from spike import _item_should_hide_from_report
    return _item_should_hide_from_report(item, rtype)


def _event_with_deduped_by_type(e: dict) -> dict:
    """Return event with change.by_type deduped (same as report)."""
    from spike import _event_with_deduped_by_type as _dedupe
    return _dedupe(e)


def _title_for_item(item: dict, rtype: str) -> str:
    """Human title for the changed item."""
    if rtype == "meetings":
        return (item.get("title") or "Meeting").strip()
    return (item.get("title") or item.get("label", "") or "").strip() or "Untitled"


def _url_for_item(item: dict, rtype: str) -> str:
    """URL for the changed item."""
    if rtype == "meetings":
        for key in ("materials_url", "agenda_url", "webex_url"):
            u = (item.get(key) or "").strip()
            if u:
                return u
        return ""
    return (item.get("url") or "").strip()


def _notes_summary(label: str, rtype: str, section_label: str) -> str:
    """Short 1-2 line summary of what changed and where."""
    type_desc = {
        "docs": "document",
        "event_links": "meeting link",
        "events": "meeting link",
        "meetings": "meeting",
    }.get(rtype, "item")
    return f"New {type_desc} in {label} ({section_label})"


def _build_one_resource(
    item: dict,
    rtype: str,
    label: str,
    org_path: list | None,
) -> dict:
    """Build a single Bubble Resource object from an added item."""
    org_path = org_path or []
    org_label = org_path[0] if org_path else "NAIC"
    parent = " › ".join(org_path + [label]) if org_path else label

    name = _title_for_item(item, rtype)
    url = _url_for_item(item, rtype)
    section_label = {"docs": "Docs", "event_links": "Meeting Links", "events": "Meeting Links", "meetings": "Meetings"}.get(rtype, "")
    notes = _notes_summary(label, rtype, section_label)

    date_val = None
    date_display = None
    if rtype == "meetings":
        dt = (item.get("date_text") or "").strip()
        tm = (item.get("time_text") or "").strip()
        if dt or tm:
            date_val = f"{dt} {tm}".strip() if (dt and tm) else (dt or tm)
            date_display = dt if dt else ""

    return {
        "archive": False,
        "Available To Vector Store": False,
        "Chunk Overlap": 200,
        "Chunk Size": 1000,
        "date": date_val,
        "Date display": date_display,
        "Name": name,
        "notes": notes,
        "Organization": org_label,
        "parent": parent,
        "Related calendar items": [],
        "URL": url,
    }


def _ensure_schema_keys(obj: dict) -> dict:
    """Return object with exactly the schema keys, in order; no extras."""
    result = {}
    for k in BUBBLE_RESOURCE_FIELDS:
        result[k] = obj.get(k)
    return result


def build_bubble_payload(change_events: list[dict], targets: list[dict] | None = None) -> list[dict]:
    """
    Build Bubble Resource create preview payload from change events.

    Only includes added items (the "+" items), not removals. Uses deduped by_type
    (same as report). Returns a list of dicts matching the Bubble Resource schema exactly.

    Args:
        change_events: List of change event dicts (target_id, label, url, org_path, change).
        targets: Optional targets list (unused for now; for future lookup).

    Returns:
        List of Bubble Resource objects with schema keys only.
    """
    payload: list[dict] = []
    for e in change_events:
        if "error" in e:
            continue
        label = e.get("label", "unknown")
        org_path = e.get("org_path")
        if isinstance(org_path, list):
            org_path = list(org_path)
        else:
            org_path = []

        deduped = _event_with_deduped_by_type(e)
        by_type = deduped.get("change", {}).get("by_type", {})

        for rtype in ("docs", "event_links", "events", "meetings"):
            added = by_type.get(rtype, {}).get("added", [])
            for item in added:
                if _item_should_hide(item, rtype):
                    continue
                obj = _build_one_resource(item, rtype, label, org_path)
                payload.append(_ensure_schema_keys(obj))

    return payload
