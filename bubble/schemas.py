"""
Bubble schema field name lists exactly as Bubble expects.
"""

from pathlib import Path

from schema_loader import load_bubble_resource_fields

# Full Resource schema: all fields for type Resource (Bubble API)
# Types: archive/Available To Vector Store (yes/no), Chunk Overlap/Size (number), date, Date display,
# Domain/Name/notes/reference/Slug (text), file/file2 (file), file name, Organization (List of Tree Nodes),
# parent (Resource), Related calendar items (List of Calendar Items), topic suggestion/Type (Tree Node),
# Type1 (List of Tree Nodes), VS Content Type, Creator (User), etc.
FULL_RESOURCE_SCHEMA_FIELDS: list[str] = [
    "archive",
    "Available To Vector Store",
    "Chunk Overlap",
    "Chunk Size",
    "date",
    "Date display",
    "Domain",
    "favorited",
    "file",
    "file name",
    "file2",
    "full date display?",
    "internal",
    "Name",
    "name-for-search",
    "notes",
    "Notes on chronicles",
    "Order",
    "Organization",
    "parent",
    "reference",
    "Related calendar items",
    "Suggestion",
    "topic suggestion",
    "Type",
    "Type1",
    "URL",
    "VS Content Type",
    "Creator",
    "Modified Date",
    "Created Date",
    "Slug",
]

# Resource schema for payload building: from CSV export or fallback (subset we populate)
_RESOURCE_CSV_PATH = Path(__file__).resolve().parent / "schema_exports" / "resources.csv"
_RESOURCE_FALLBACK = [
    "archive",
    "Available To Vector Store",
    "Chunk Overlap",
    "Chunk Size",
    "date",
    "Date display",
    "Name",
    "notes",
    "Organization",
    "parent",
    "Related calendar items",
    "URL",
]
RESOURCE_SCHEMA_FIELDS: list[str] = load_bubble_resource_fields(_RESOURCE_CSV_PATH) or _RESOURCE_FALLBACK

# Full Calendar Item schema: all fields for type Calendar Item (Bubble API)
# Types: Agenda (List of Resources), attached agenda items (List of Agenda Items), color/date/event description/
# location/Outlook Event UID/outlook_icaluid/phone_number_and_ac/subtopic/Timezone Code/title (text),
# date/End time/Outlook last sync (date), full day/has topic (yes/no), length (Time Lengths),
# NAIC Date/Meeting Type, NAIC Group (legacy), NAIC Group (tree node), Relevant Documents (List of Chronicle Links)
FULL_CALENDAR_ITEM_SCHEMA_FIELDS: list[str] = [
    "Agenda",
    "alerts",
    "attached agenda items",
    "color",
    "date",
    "End time",
    "event description",
    "full day",
    "has topic",
    "length",
    "location",
    "NAIC Date/Meeting Type",
    "NAIC Group (legacy)",
    "NAIC Group (tree node)",
    "no agenda type",
    "Outlook Event UID",
    "Outlook last sync",
    "outlook_icaluid",
    "phone_number_and_ac…",
    "Relevant Documents",
    "subtopic",
    "Timezone Code",
    "title",
]

# Calendar Item schema: same as full (Bubble API)
CALENDAR_ITEM_SCHEMA_FIELDS: list[str] = FULL_CALENDAR_ITEM_SCHEMA_FIELDS
