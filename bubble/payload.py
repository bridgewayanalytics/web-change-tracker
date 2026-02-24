"""
Build Bubble Resource and Calendar Item payloads from change events.
"""

import logging
import re

from bubble.schemas import CALENDAR_ITEM_SCHEMA_FIELDS, FULL_RESOURCE_SCHEMA_FIELDS

log = logging.getLogger(__name__)

_AGENDA_LINK_KEYWORDS = ("agenda", "materials", "minutes", "call", "webex")


def validate_payload(schema_fields: list[str], obj: dict) -> dict:
    """
    Ensure NO extra keys and all schema keys are present.
    Missing keys are set to None (or [] for list fields).
    """
    result: dict = {}
    for k in schema_fields:
        if k in obj:
            result[k] = obj[k]
        elif k in (
            "Related calendar items",
            "attached agenda items",
            "Relevant Documents",
            "Agenda",
            "Organization",
            "Type1",
        ):
            result[k] = obj.get(k) if obj.get(k) is not None else []
        else:
            result[k] = None
    return result


def _item_should_hide(item: dict, rtype: str) -> bool:
    """True if item should be excluded (denied URLs)."""
    from spike import _item_should_hide_from_report
    return _item_should_hide_from_report(item, rtype)


def _event_with_deduped_by_type(e: dict) -> dict:
    """Return event with change.by_type deduped (same as report)."""
    from spike import _event_with_deduped_by_type as _dedupe
    return _dedupe(e)


def _build_one_resource(item: dict, rtype: str, label: str, org_path: list) -> dict:
    """Build a single Bubble Resource object (docs, event_links, events only)."""
    org_label = org_path[0] if org_path else "NAIC"
    parent = " › ".join(org_path + [label]) if org_path else label
    name = (item.get("title") or item.get("label", "") or "").strip() or "Untitled"
    url = (item.get("url") or "").strip()
    section_label = {"docs": "Docs", "event_links": "Meeting Links", "events": "Meeting Links"}.get(rtype, "")
    notes = f"New {section_label.lower()[:-1]} in {label} ({section_label})"

    return {
        "archive": False,
        "Available To Vector Store": False,
        "Chunk Overlap": 200,
        "Chunk Size": 1000,
        "date": None,
        "Date display": None,
        "Name": name,
        "notes": notes,
        "Organization": org_label,
        "parent": parent,
        "Related calendar items": [],
        "URL": url,
    }


def _link_suggests_agenda_materials_webex(item: dict) -> bool:
    """True if link label or URL suggests agenda, materials, webex, or call."""
    text = " ".join([
        (item.get("title") or ""),
        (item.get("label") or ""),
        (item.get("url") or ""),
    ]).lower()
    return any(kw in text for kw in _AGENDA_LINK_KEYWORDS)


def _date_hints_from_text(text: str) -> set[str]:
    """Extract date-like substrings for matching (e.g. january 15, 2025-01-15, 01/15)."""
    if not text:
        return set()
    text = text.lower()
    hints: set[str] = set()
    # Month DD, YYYY or Month D, YYYY
    for m in re.finditer(r"(january|february|march|april|may|june|july|august|september|october|november|december)\s*(\d{1,2}),?\s*(\d{4})?", text):
        parts = [m.group(1), m.group(2)]
        if m.group(3):
            parts.append(m.group(3))
        hints.add(" ".join(parts))
    # YYYY-MM-DD
    for m in re.finditer(r"(\d{4})[-_](\d{2})[-_](\d{2})", text):
        hints.add(f"{m.group(1)}-{m.group(2)}-{m.group(3)}")
    # MM/DD/YYYY or MM-DD-YYYY
    for m in re.finditer(r"(\d{1,2})[/\-](\d{1,2})[/\-](\d{4})", text):
        hints.add(f"{m.group(3)}-{m.group(1).zfill(2)}-{m.group(2).zfill(2)}")
    return hints


def _meeting_date_hints(meeting: dict) -> set[str]:
    """Extract date hints from meeting date_text for matching."""
    date_text = (meeting.get("date_text") or "").strip()
    return _date_hints_from_text(date_text)


def _associate_links_to_meetings(
    meetings: list[dict],
    meeting_links: list[dict],
) -> dict[int, list[dict]]:
    """
    Associate agenda-like links to meetings. Returns {meeting_index: [links]}.
    Prefer links containing meeting date; fallback to first upcoming meeting.
    """
    result: dict[int, list[dict]] = {i: [] for i in range(len(meetings))}
    meeting_hints = [_meeting_date_hints(m) for m in meetings]

    for link in meeting_links:
        link_hints = _date_hints_from_text(
            " ".join([(link.get("title") or ""), (link.get("url") or "")])
        )
        best_idx = 0
        if link_hints and meeting_hints:
            for i, mh in enumerate(meeting_hints):
                if mh and link_hints & mh:
                    best_idx = i
                    break
        result[best_idx].append(link)

    return result


def _build_one_calendar_item(
    meeting: dict,
    label: str,
    org_path: list,
    agenda_links: list[dict] | None = None,
) -> dict:
    """Build a single Bubble Calendar Item object from a meeting."""
    title = (meeting.get("title") or "").strip() or f"{label} Meeting"
    date_text = (meeting.get("date_text") or "").strip()
    time_text = (meeting.get("time_text") or "").strip()
    expected_duration = (meeting.get("expected_duration") or "").strip()
    notes_val = (meeting.get("notes") or "").strip()

    # date: start_datetime if available, else date_text + time_text, else null
    date_val = None
    if date_text or time_text:
        date_val = f"{date_text} {time_text}".strip() if (date_text and time_text) else (date_text or time_text)

    # End time: end_datetime if available else null (we don't have it)
    end_time = None

    # length: from expected_duration if available
    length_val = expected_duration or None

    # Agenda: list of resource-like dicts {url, title} from associated meeting links
    agenda_list: list[dict] = []
    if agenda_links:
        for item in agenda_links:
            url = (item.get("url") or "").strip()
            if url:
                agenda_list.append({
                    "url": url,
                    "title": (item.get("title") or item.get("label") or "").strip() or "Agenda/Materials",
                })

    obj: dict = {
        "Agenda": agenda_list,
        "attached agenda items": [],
        "color": None,
        "date": date_val,
        "End time": end_time,
        "event description": notes_val,
        "full day": "no",
        "has topic": None,
        "length": length_val,
        "location": None,
        "NAIC Date/Meeting Type": None,
        "NAIC Group (legacy)": None,
        "NAIC Group (tree node)": " › ".join(org_path + [label]) if org_path else label,
        "no agenda type": None,
        "Outlook Event UID": None,
        "Outlook last sync": None,
        "outlook_icaluid": None,
        "phone_number_and_ac…": None,
        "Relevant Documents": [],
        "subtopic": None,
        "Timezone Code": "America/New_York",
        "title": title,
    }
    return obj


def build_resource_payload(diff: list[dict], targets: list[dict] | None = None) -> list[dict]:
    """
    Build Resource payload from added docs, event_links, and events.
    Meetings create Calendar Items, not Resources.
    """
    payload: list[dict] = []
    for e in diff:
        if "error" in e:
            continue
        label = e.get("label", "unknown")
        org_path = list(e.get("org_path") or [])

        deduped = _event_with_deduped_by_type(e)
        by_type = deduped.get("change", {}).get("by_type", {})

        for rtype in ("docs", "event_links", "events"):
            added = by_type.get(rtype, {}).get("added", [])
            for item in added:
                if _item_should_hide(item, rtype):
                    continue
                obj = _build_one_resource(item, rtype, label, org_path)
                payload.append(validate_payload(FULL_RESOURCE_SCHEMA_FIELDS, obj))

    return payload


def build_calendar_item_payload(diff: list[dict], targets: list[dict] | None = None) -> list[dict]:
    """
    Build Calendar Item payload from added meetings.
    Associates agenda/materials/webex meeting links (event_links, events) to meetings when possible.
    """
    payload: list[dict] = []
    total_attached = 0

    for e in diff:
        if "error" in e:
            continue
        label = e.get("label", "unknown")
        org_path = list(e.get("org_path") or [])

        deduped = _event_with_deduped_by_type(e)
        by_type = deduped.get("change", {}).get("by_type", {})
        meetings_added = by_type.get("meetings", {}).get("added", [])
        meeting_links: list[dict] = []
        for rtype in ("event_links", "events"):
            for item in by_type.get(rtype, {}).get("added", []):
                if _item_should_hide(item, rtype):
                    continue
                if _link_suggests_agenda_materials_webex(item):
                    meeting_links.append(item)

        visible_meetings = [m for m in meetings_added if not _item_should_hide(m, "meetings")]
        if not visible_meetings:
            continue

        association = _associate_links_to_meetings(visible_meetings, meeting_links)

        for i, meeting in enumerate(visible_meetings):
            agenda_for_meeting = association.get(i, [])
            total_attached += len(agenda_for_meeting)
            obj = _build_one_calendar_item(meeting, label, org_path, agenda_links=agenda_for_meeting)
            payload.append(validate_payload(CALENDAR_ITEM_SCHEMA_FIELDS, obj))

    if total_attached > 0:
        log.info("Attached %d meeting link(s) to Calendar Item(s)", total_attached)

    return payload


def build_resource_context(diff: list[dict]) -> list[dict]:
    """Return per-item context (org_id, org_path, label, url) in same order as build_resource_payload."""
    ctx: list[dict] = []
    for e in diff:
        if "error" in e:
            continue
        label = e.get("label", "unknown")
        org_path = list(e.get("org_path") or [])
        org_id = e.get("org_id")
        url = (e.get("url") or "").strip()
        deduped = _event_with_deduped_by_type(e)
        by_type = deduped.get("change", {}).get("by_type", {})
        for rtype in ("docs", "event_links", "events"):
            added = by_type.get(rtype, {}).get("added", [])
            for item in added:
                if _item_should_hide(item, rtype):
                    continue
                ctx.append({
                    "org_id": org_id,
                    "org_path": org_path,
                    "label": label,
                    "url": url,
                })
    return ctx


def build_calendar_item_context(diff: list[dict]) -> list[dict]:
    """Return per-item context (org_id, org_path, label, url) in same order as build_calendar_item_payload."""
    ctx: list[dict] = []
    for e in diff:
        if "error" in e:
            continue
        label = e.get("label", "unknown")
        org_path = list(e.get("org_path") or [])
        org_id = e.get("org_id")
        url = (e.get("url") or "").strip()
        deduped = _event_with_deduped_by_type(e)
        by_type = deduped.get("change", {}).get("by_type", {})
        meetings_added = by_type.get("meetings", {}).get("added", [])
        visible_meetings = [m for m in meetings_added if not _item_should_hide(m, "meetings")]
        for _ in visible_meetings:
            ctx.append({
                "org_id": org_id,
                "org_path": org_path,
                "label": label,
                "url": url,
            })
    return ctx
