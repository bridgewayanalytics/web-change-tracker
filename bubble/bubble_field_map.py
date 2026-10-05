"""
Bubble Data API write field name mappings.

Primary structure: dicts keyed by stable registry ID (the `id` field in DynamoDB's
`_column_registry`). These IDs never change even when human-readable labels are renamed
in Bubble Admin. The Bubble field name (the value) is what changes if Mori updates the
Bubble data model — update the value here and the whole pipeline picks it up.

Bubble write field names are the exact display names from GET responses / Swagger Body
definitions (confirmed via live API tests 2026-10-05). The underscore convention
(e.g. title_text, orgs__list_custom_organization) is WRONG — Bubble rejects those.

Usage in classifier / executor:
    from bubble.bubble_field_map import CALENDARITEM, LIBRARYITEM, AGENDAITEM
    from bubble.bubble_field_map import CalendarItemField as CI, LibraryItemField as LI

    # Dict lookup — explicit link to stable registry ID:
    field_ids[CALENDARITEM["event_title"]] = title

    # Class constant — convenient for system fields not derived from agent output:
    field_ids[CI.SPACE] = space_id
    field_ids[LI.EIDARIX_REF] = agent_call_id
"""

# ---------------------------------------------------------------------------
# Primary mapping: stable registry ID → Bubble Data API write field name
# These cover only the agent output fields that map directly to a Bubble field.
# Fields that require executor logic (ID resolution, datetime conversion, etc.)
# are noted in comments but not listed here — they are set by the executor, not
# derived mechanically from the alert dict.
# ---------------------------------------------------------------------------

# calendaritem — agent output fields → Bubble write field names
CALENDARITEM: dict[str, str] = {
    "event_title":                      "title",
    "event_start_date_time":            "date",         # ET ISO 8601; executor converts to UTC
    "event_end_date_time":              "End time",     # ET ISO 8601; executor converts to UTC
    "event_is_full_day":                "full day",     # "Full Day" string → True bool at sync
    "event_url":                        "location",
    "event_call_in_number_access_code": "phone_number_and_access_code",
    "organization":                     "Orgs ",        # trailing space; executor resolves names→IDs
    # Fields set by the executor (not direct agent output):
    #   "Topics - dt"           → resolved chronicle topic IDs
    #   "attached agenda items" → resolved agendaitem IDs
    #   "Agenda"                → resolved libraryitem IDs
    #   "type"                  → calendaritemtype _id from lookup
    #   "Timezone Code"         → hardcoded "America/New_York"
    #   "Space"                 → space _id constant
    #   "Eiderix Ref"           → agent_call_id
}

# libraryitem — agent output fields → Bubble write field names
LIBRARYITEM: dict[str, str] = {
    "library_item_preliminary_title":   "Name",         # executor extracts .title subfield
    "library_item_url":                 "URL",
    "library_items_file_name":          "file name",
    "organization":                     "Organizations", # executor resolves names→IDs
    # Fields set by the executor:
    #   "Topics - dt"    → resolved chronicle topic IDs (union of agenda items + doc extraction)
    #   "Agenda items"   → resolved agendaitem IDs
    #   "Type - DT"      → libraryitemtype _id from lookup
    #   "date"           → event_start_date_time as UTC midnight
    #   "Date display"   → "Full date"
    #   "Status"         → "Active"
    #   "Space"          → space _id constant
    #   "Eiderix Ref"    → agent_call_id
    # Fields set by enrich_with_doc_extraction:
    #   "summary"        → document_description from doc extraction
    #   "date"           → date_published from doc extraction (if no event date)
    #   "Topics - dt"    → topics from doc extraction
}

# agendaitem — subfield name within each agenda item dict → Bubble write field name
# These are the inner keys of agenda_item_title_chronicle_topics[i], not top-level agent fields.
AGENDAITEM: dict[str, str] = {
    "agenda_item_title":  "BA title",   # from agenda_item_title_chronicle_topics[i].agenda_item_title
    "official_title":     "NAIC Title", # from agenda_item_title_official[i].official_title
    "standardized_id":    "BA Ref #",  # from agenda_item_standardized_id[i].standardized_id
    # Fields set by the executor:
    #   "topics - dt"  → resolved chronicle topic IDs per agenda item
    #   "space"        → space _id constant (lowercase on agendaitem)
    #   "Eiderix Ref"  → agent_call_id
}


# ---------------------------------------------------------------------------
# Convenience class constants derived from the dicts above, plus system fields.
# Executor code uses these when building payloads — CI.TOPICS, LI.SPACE, etc.
# The agent-mapped constants (TITLE, DATE, ORGS, …) are derived from the dicts
# so they're guaranteed to stay in sync with the primary mapping.
# ---------------------------------------------------------------------------

class CalendarItemField:
    """Bubble write field names for calendaritem."""
    # Derived from CALENDARITEM dict — always in sync
    TITLE    = CALENDARITEM["event_title"]
    DATE     = CALENDARITEM["event_start_date_time"]
    END_TIME = CALENDARITEM["event_end_date_time"]
    FULL_DAY = CALENDARITEM["event_is_full_day"]
    LOCATION = CALENDARITEM["event_url"]
    PHONE    = CALENDARITEM["event_call_in_number_access_code"]
    ORGS     = CALENDARITEM["organization"]
    # System fields (set by executor, not from agent output)
    TOPICS        = "Topics - dt"
    AGENDA_ITEMS  = "attached agenda items"
    AGENDA        = "Agenda"
    TYPE          = "type"
    TIMEZONE      = "Timezone Code"
    SPACE         = "Space"
    EIDARIX_REF   = "Eiderix Ref"


class LibraryItemField:
    """Bubble write field names for libraryitem."""
    # Derived from LIBRARYITEM dict — always in sync
    NAME      = LIBRARYITEM["library_item_preliminary_title"]
    URL       = LIBRARYITEM["library_item_url"]
    FILE_NAME = LIBRARYITEM["library_items_file_name"]
    ORGS      = LIBRARYITEM["organization"]
    # System fields (set by executor or enrich_with_doc_extraction)
    TOPICS        = "Topics - dt"
    AGENDA_ITEMS  = "Agenda items"
    TYPE          = "Type - DT"
    DATE          = "date"
    DATE_DISPLAY  = "Date display"
    SUMMARY       = "summary"
    STATUS        = "Status"
    SPACE         = "Space"
    EIDARIX_REF   = "Eiderix Ref"


class AgendaItemField:
    """Bubble write field names for agendaitem.

    Note: `space` is lowercase on agendaitem (unlike `Space` on calendaritem/libraryitem).
    """
    # Derived from AGENDAITEM dict — always in sync
    TITLE      = AGENDAITEM["agenda_item_title"]
    NAIC_TITLE = AGENDAITEM["official_title"]
    REF        = AGENDAITEM["standardized_id"]
    # System fields
    TOPICS      = "topics - dt"   # lowercase — exact Bubble display name on agendaitem
    SPACE       = "space"         # lowercase — exact Bubble display name on agendaitem
    EIDARIX_REF = "Eiderix Ref"
