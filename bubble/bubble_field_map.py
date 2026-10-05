"""
Bubble Data API write field names for each object type.

These are the exact display names the Bubble Data API accepts in POST/PATCH body.
Confirmed via live API tests 2026-10-05 against the test space.

Rule: write field name == display name as returned by GET / Swagger Body definitions.
The underscore naming convention (e.g. title_text, orgs__list_custom_organization)
is WRONG for POST/PATCH — Bubble returns "Unrecognized field" for those keys.

Usage:
    from bubble.bubble_field_map import CalendarItemField as CI
    from bubble.bubble_field_map import LibraryItemField as LI
    from bubble.bubble_field_map import AgendaItemField as AI

    field_ids[CI.TITLE] = title
    field_ids[LI.NAME]  = doc_title
    field_ids[AI.TITLE] = agenda_title
"""


class CalendarItemField:
    """Bubble write field names for the `calendaritem` type."""

    TITLE       = "title"
    DATE        = "date"                           # start datetime — convert ET → UTC before send
    END_TIME    = "End time"                       # end datetime — convert ET → UTC before send
    FULL_DAY    = "full day"                       # boolean
    TYPE        = "type"                           # → calendaritemtype._id
    ORGS        = "Orgs "                          # trailing space — exact Bubble display name
    TOPICS      = "Topics - dt"                   # → chronicletopic._id list
    AGENDA_ITEMS = "attached agenda items"         # → agendaitem._id list
    AGENDA      = "Agenda"                        # → libraryitem._id list
    TIMEZONE    = "Timezone Code"                 # always "America/New_York"
    PHONE       = "phone_number_and_access_code"
    LOCATION    = "location"                      # meeting URL
    DESCRIPTION = "event description"
    SPACE       = "Space"                         # → space._id
    EIDARIX_REF = "Eiderix Ref"                   # our pipeline's agent_call_id


class LibraryItemField:
    """Bubble write field names for the `libraryitem` type."""

    NAME         = "Name"
    URL          = "URL"
    FILE_NAME    = "file name"
    TYPE         = "Type - DT"                    # → libraryitemtype._id
    ORGS         = "Organizations"                # → organization._id list (no trailing space)
    TOPICS       = "Topics - dt"                  # → chronicletopic._id list
    AGENDA_ITEMS = "Agenda items"                 # → agendaitem._id list
    DATE         = "date"                         # UTC midnight: YYYY-MM-DDT00:00:00.000Z
    DATE_DISPLAY = "Date display"                 # option set: "Full date" | "Month, Year" | "Year"
    SUMMARY      = "summary"
    STATUS       = "Status"                       # option set: always "Active" for new records
    SPACE        = "Space"                        # → space._id
    EIDARIX_REF  = "Eiderix Ref"                  # our pipeline's agent_call_id


class AgendaItemField:
    """Bubble write field names for the `agendaitem` type.

    Note: `space` is lowercase on agendaitem (unlike `Space` on calendaritem/libraryitem).
    """

    TITLE       = "BA title"                      # Bridgeway Analytics title
    NAIC_TITLE  = "NAIC Title"                    # official NAIC agenda title
    REF         = "BA Ref #"                      # reference ID e.g. "LATF#APF-2025-14"
    TOPICS      = "topics - dt"                   # → chronicletopic._id list (lowercase field name)
    SPACE       = "space"                         # lowercase — exact Bubble display name
    EIDARIX_REF = "Eiderix Ref"                   # our pipeline's agent_call_id
