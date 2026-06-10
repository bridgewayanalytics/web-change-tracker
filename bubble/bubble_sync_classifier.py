"""
Classify a page-change alert into Bubble sync actions.

Pure function — no I/O, no API calls. Called by spike.py to stamp bubble_action
on each alert dict before storage, and by the dashboard preview endpoint.

bubble_action field structure (stored on alert dict when applicable=True):
{
  "event": "create" | "update" | null,
  "library_item": "create" | "update" | null,
  "agenda_items": true | false,
  "event_preview": { title, start_datetime, end_datetime, group, url, call_in, match_key },
  "library_item_preview": { title, url, filename, type, group },
  "agenda_item_previews": [{ title, chronicle_topics }],
  "notes": <alert_type string>
}

When applicable=False (No Meaningful Change, carousel reordering) the field is not set.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

_NOT_APPLICABLE = frozenset({
    "No Meaningful Change",
    "Alert not relevant - the change was limited to carrousel or reordering of content",
})

# Maps alert_type → (event_action, library_item_action, create_agenda_items)
_TYPE_MAP: dict[str, tuple[str | None, str | None, bool]] = {
    "New Meeting":                              ("create", None,     False),
    "Updated Meeting":                          ("update", None,     False),
    "New Agenda":                               ("update", "create", True),
    "New Materials":                            ("update", "create", False),
    "New Agenda & Materials":                   ("update", "create", True),
    "Updated Agenda":                           ("update", "update", True),
    "Updated Materials":                        ("update", "update", False),
    "Updated Agenda & Materials":               ("update", "update", True),
    "New Request for Comment":                  ("create", "create", False),
    "Updated Request for Comment":              ("update", "update", False),
    "New Effective Date":                       ("create", "create", False),
    "Updated Effective Date":                   ("update", "update", False),
    "New or Updated Report or Other Resource":  ("update", "create", False),
    "Other":                                    ("update", "create", False),
}

# Maps alert_type → human-readable Library Item type label
_LIBRARY_ITEM_TYPE: dict[str, str] = {
    "New Agenda":                               "Agenda",
    "New Materials":                            "Materials",
    "New Agenda & Materials":                   "Agenda & Materials",
    "Updated Agenda":                           "Agenda",
    "Updated Materials":                        "Materials",
    "Updated Agenda & Materials":               "Agenda & Materials",
    "New Request for Comment":                  "Request for Comment",
    "Updated Request for Comment":              "Request for Comment",
    "New Effective Date":                       "Adopted Guideline",
    "Updated Effective Date":                   "Adopted Guideline",
    "New or Updated Report or Other Resource":  "Report / Resource",
    "Other":                                    "Document",
}

_NA_VALUES = frozenset({"n/a", "n/a.", "-", "", "none"})


def _is_na(val: object) -> bool:
    if val is None:
        return True
    return str(val).strip().lower() in _NA_VALUES


def _str(val: object) -> str:
    if val is None:
        return ""
    return str(val).strip()


def _org_list(alert: dict) -> list[str]:
    org = alert.get("organization")
    if isinstance(org, list):
        return [str(o) for o in org if o]
    if org and not _is_na(org):
        return [str(org)]
    return []


def _lib_title(alert: dict) -> str:
    raw = alert.get("library_item_preliminary_title")
    if isinstance(raw, dict):
        return _str(raw.get("title") or raw.get("library_item_title") or "")
    return _str(raw)


def _build_event_preview(alert: dict) -> dict:
    title = _str(alert.get("event_title"))
    start = _str(alert.get("event_start_date_time"))
    end = _str(alert.get("event_end_date_time"))
    org = _org_list(alert)
    url = _str(alert.get("event_url"))
    call_in = _str(alert.get("event_call_in_number_access_code"))

    # Match key: first org + date portion of start datetime
    date_part = start[:10] if len(start) >= 10 else start
    primary_org = org[0] if org else _str(alert.get("alert_url") or "")
    match_key = f"{primary_org} | {date_part}" if primary_org or date_part else ""

    return {
        "title": title if not _is_na(title) else "",
        "start_datetime": start if not _is_na(start) else "",
        "end_datetime": end if not _is_na(end) else "",
        "group": org,
        "url": url if not _is_na(url) else "",
        "call_in": call_in if not _is_na(call_in) else "",
        "match_key": match_key,
    }


def _build_library_item_preview(alert: dict, alert_type: str) -> dict:
    title = _lib_title(alert)
    url = _str(alert.get("library_item_url"))
    filename = _str(alert.get("library_items_file_name"))
    org = _org_list(alert)
    item_type = _LIBRARY_ITEM_TYPE.get(alert_type, "Document")

    return {
        "title": title if not _is_na(title) else "",
        "url": url if not _is_na(url) else "",
        "filename": filename if not _is_na(filename) else "",
        "type": item_type,
        "group": org,
    }


def _build_agenda_previews(alert: dict) -> list[dict]:
    items = alert.get("agenda_item_title_and_chronicle_topics")
    if not isinstance(items, list):
        return []
    result = []
    for item in items:
        if not isinstance(item, dict):
            continue
        title = _str(item.get("agenda_item_title") or item.get("title") or "")
        if _is_na(title):
            continue
        topics = item.get("chronicle_topics") or []
        if not isinstance(topics, list):
            topics = []
        result.append({"title": title, "chronicle_topics": [str(t) for t in topics if t]})
    return result


@dataclass
class BubbleSyncPlan:
    applicable: bool
    event_action: Literal["create", "update"] | None = None
    library_item_action: Literal["create", "update"] | None = None
    create_agenda_items: bool = False
    event_preview: dict = field(default_factory=dict)
    library_item_preview: dict = field(default_factory=dict)
    agenda_item_previews: list = field(default_factory=list)
    notes: str = ""

    def to_dict(self) -> dict:
        """Serialize to dict for storage on alert row."""
        return {
            "event": self.event_action,
            "library_item": self.library_item_action,
            "agenda_items": self.create_agenda_items,
            "event_preview": self.event_preview,
            "library_item_preview": self.library_item_preview,
            "agenda_item_previews": self.agenda_item_previews,
            "notes": self.notes,
        }


def classify_alert(alert: dict) -> BubbleSyncPlan:
    """
    Classify an alert dict into a BubbleSyncPlan.

    Returns BubbleSyncPlan(applicable=False) for irrelevant alerts
    (No Meaningful Change, carousel reordering).
    """
    alert_type = _str(alert.get("alert_type"))

    if alert_type in _NOT_APPLICABLE:
        return BubbleSyncPlan(applicable=False)

    ev_action, lib_action, agenda = _TYPE_MAP.get(alert_type, ("update", "create", False))

    return BubbleSyncPlan(
        applicable=True,
        event_action=ev_action,  # type: ignore[arg-type]
        library_item_action=lib_action,  # type: ignore[arg-type]
        create_agenda_items=agenda,
        event_preview=_build_event_preview(alert),
        library_item_preview=_build_library_item_preview(alert, alert_type) if lib_action else {},
        agenda_item_previews=_build_agenda_previews(alert) if agenda else [],
        notes=alert_type,
    )
