"""
Classify a page-change alert into Bubble sync actions.

Pure function — no I/O, no API calls. Called by spike.py to stamp bubble_action
on each alert dict before storage, and by the dashboard preview endpoint.

bubble_action field structure (stored on alert dict when applicable=True):
{
  "event": "create" | "update" | null,
  "library_item": "create" | "update" | null,
  "agenda_items": true | false,
  "event_preview": {
    title, start_datetime, end_datetime, group, url, call_in, match_key,
    what_changes,           # kept for backward compat with old stored rows
    fields,                 # display names → values  (for modal FieldTable)
    field_ids,              # Bubble field IDs → values (for executor; org names, not IDs)
    match_search,           # for UPDATE: {"org": ..., "date": "YYYY-MM-DD"}
  },
  "library_item_preview": {
    title, url, filename, type, group,
    what_changes,           # kept for backward compat
    fields,                 # display names → values
    field_ids,              # Bubble field IDs → values
    match_search,           # for UPDATE: {"url": ..., "title": ...}
  },
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

# For UPDATE calendaritem, the Agenda field label to show in the modal
_EVENT_AGENDA_LINK_LABEL: dict[str, str] = {
    "New Agenda":                               "→ Link new agenda document",
    "New Materials":                            "→ Link new materials document",
    "New Agenda & Materials":                   "→ Link new agenda/materials documents",
    "Updated Agenda":                           "→ Update agenda document reference",
    "Updated Materials":                        "→ Update materials document reference",
    "Updated Agenda & Materials":               "→ Update agenda/materials document references",
    "New Request for Comment":                  "→ Link new RFC document",
    "Updated Request for Comment":              "→ Update RFC document reference",
    "New Effective Date":                       "→ Link adopted guideline document",
    "Updated Effective Date":                   "→ Update guideline document reference",
    "New or Updated Report or Other Resource":  "→ Link document or resource",
    "Other":                                    "→ Update document reference",
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


# what_changes kept for backward compat with old stored rows that lack the fields/field_ids keys
_EVENT_UPDATE_CHANGES: dict[str, list[str]] = {
    "Updated Meeting":              ["Update date/time, location, or call-in details"],
    "New Agenda":                   ["Link new agenda document", "Append agenda items"],
    "New Materials":                ["Link new materials document"],
    "New Agenda & Materials":       ["Link new agenda/materials documents", "Append agenda items"],
    "Updated Agenda":               ["Update existing agenda document reference", "Update agenda items"],
    "Updated Materials":            ["Update existing materials document reference"],
    "Updated Agenda & Materials":   ["Update existing documents", "Update agenda items"],
    "New Request for Comment":      ["Link RFC document to event"],
    "Updated Request for Comment":  ["Update RFC document reference"],
    "New Effective Date":           ["Link adopted guideline document"],
    "Updated Effective Date":       ["Update guideline document reference"],
    "New or Updated Report or Other Resource": ["Link document or resource"],
    "Other":                        ["Update event record"],
}

_LIB_UPDATE_CHANGES: dict[str, list[str]] = {
    "Updated Materials":            ["Replace file/URL with new version"],
    "Updated Agenda":               ["Replace file/URL with new version"],
    "Updated Agenda & Materials":   ["Replace file/URL with new version"],
    "Updated Request for Comment":  ["Replace document with new version"],
    "Updated Effective Date":       ["Replace document with new version"],
}


def _build_event_preview(alert: dict, alert_type: str, event_action: str | None) -> dict:
    title = _str(alert.get("event_title"))
    start = _str(alert.get("event_start_date_time"))
    end = _str(alert.get("event_end_date_time"))
    org = _org_list(alert)
    url = _str(alert.get("event_url"))
    call_in = _str(alert.get("event_call_in_number_access_code"))
    is_full_day = _str(alert.get("event_is_full_day")).lower() == "full day"

    date_part = start[:10] if len(start) >= 10 else start
    primary_org = org[0] if org else ""
    match_key = f"{primary_org} | {date_part}" if (primary_org or date_part) else ""

    # backward-compat
    what_changes = _EVENT_UPDATE_CHANGES.get(alert_type, []) if event_action == "update" else []

    # -- Compute fields, field_ids, match_search per action type --
    if event_action == "create":
        fields: dict[str, str] = {}
        if not _is_na(title):
            fields["Title"] = title
        if not _is_na(start):
            fields["Start"] = start
        if not _is_na(end):
            fields["End"] = end
        if org:
            fields["Groups"] = ", ".join(org)
        if not _is_na(call_in):
            fields["Call-in"] = call_in
        fields["Timezone"] = "America/New_York"

        field_ids: dict = {}
        if not _is_na(title):
            field_ids["title_text"] = title
        if not _is_na(start):
            field_ids["date_date"] = start
        if not _is_na(end):
            field_ids["length_end_time_date"] = end
        if is_full_day:
            field_ids["full_day_boolean"] = True
        if org:
            field_ids["orgs__list_custom_organization"] = org  # names; executor resolves to IDs
        if not _is_na(call_in):
            field_ids["phone_number_and_access_code_text"] = call_in
        field_ids["timezone_code_text"] = "America/New_York"

        match_search: dict = {}

    elif event_action == "update":
        match_search = {}
        if primary_org or date_part:
            match_search = {"org": primary_org, "date": date_part}

        if alert_type == "Updated Meeting":
            # Update date/time, call-in, and potentially title/orgs
            fields = {}
            if not _is_na(title):
                fields["Title"] = title
            if not _is_na(start):
                fields["Start"] = start
            if not _is_na(end):
                fields["End"] = end
            if org:
                fields["Groups"] = ", ".join(org)
            if not _is_na(call_in):
                fields["Call-in"] = call_in

            field_ids = {}
            if not _is_na(title):
                field_ids["title_text"] = title
            if not _is_na(start):
                field_ids["date_date"] = start
            if not _is_na(end):
                field_ids["length_end_time_date"] = end
            if org:
                field_ids["orgs__list_custom_organization"] = org
            if not _is_na(call_in):
                field_ids["phone_number_and_access_code_text"] = call_in
            field_ids["timezone_code_text"] = "America/New_York"
        else:
            # Only change is linking/updating a library item — executor handles this dynamically
            link_label = _EVENT_AGENDA_LINK_LABEL.get(alert_type, "→ Update document reference")
            fields = {"Agenda": link_label}
            field_ids = {}  # relevant_resources_list_custom_resource filled by executor after lib item

    else:
        fields = {}
        field_ids = {}
        match_search = {}

    return {
        # Existing keys — kept for backward compat with old stored JSONL rows
        "title": title if not _is_na(title) else "",
        "start_datetime": start if not _is_na(start) else "",
        "end_datetime": end if not _is_na(end) else "",
        "group": org,
        "url": url if not _is_na(url) else "",
        "call_in": call_in if not _is_na(call_in) else "",
        "match_key": match_key,
        "what_changes": what_changes,
        # New keys
        "fields": fields,
        "field_ids": field_ids,
        "match_search": match_search,
    }


def _build_library_item_preview(alert: dict, alert_type: str, lib_action: str | None) -> dict:
    title = _lib_title(alert)
    url = _str(alert.get("library_item_url"))
    filename = _str(alert.get("library_items_file_name"))
    org = _org_list(alert)
    item_type = _LIBRARY_ITEM_TYPE.get(alert_type, "Document")

    # backward-compat
    what_changes = _LIB_UPDATE_CHANGES.get(alert_type, ["Update metadata and references"]) if lib_action == "update" else []

    if lib_action == "create":
        fields: dict[str, str] = {}
        if not _is_na(title):
            fields["Name"] = title
        if not _is_na(url):
            fields["URL"] = url
        if not _is_na(filename):
            fields["File"] = filename
        fields["Type"] = item_type
        if org:
            fields["Organizations"] = ", ".join(org)
        fields["Status"] = "Active"

        field_ids: dict = {}
        if not _is_na(title):
            field_ids["name_text"] = title
        if not _is_na(url):
            field_ids["url_text"] = url
        if not _is_na(filename):
            field_ids["file_name_text"] = filename
        if org:
            field_ids["organizations_list_custom_organization"] = org  # names; executor resolves to IDs
        field_ids["status_option_status"] = "Active"

        match_search: dict = {}

    elif lib_action == "update":
        fields = {}
        if not _is_na(url):
            fields["URL"] = url
        if not _is_na(filename):
            fields["File"] = filename

        field_ids = {}
        if not _is_na(url):
            field_ids["url_text"] = url
        if not _is_na(filename):
            field_ids["file_name_text"] = filename

        match_search = {}
        if not _is_na(url):
            match_search["url"] = url
        if not _is_na(title):
            match_search["title"] = title

    else:
        fields = {}
        field_ids = {}
        match_search = {}

    return {
        # Existing keys — backward compat
        "title": title if not _is_na(title) else "",
        "url": url if not _is_na(url) else "",
        "filename": filename if not _is_na(filename) else "",
        "type": item_type,
        "group": org,
        "what_changes": what_changes,
        # New keys
        "fields": fields,
        "field_ids": field_ids,
        "match_search": match_search,
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
        event_preview=_build_event_preview(alert, alert_type, ev_action),
        library_item_preview=_build_library_item_preview(alert, alert_type, lib_action) if lib_action else {},
        agenda_item_previews=_build_agenda_previews(alert) if agenda else [],
        notes=alert_type,
    )
