"""
Bubble Resources schema: field names for the Resources type.

BUBBLE_RESOURCE_FIELDS is loaded from schemas/bubble/Resources.csv at runtime.
Falls back to a hardcoded list if the file is missing.
"""

from pathlib import Path

from schema_loader import load_bubble_resource_fields

# Fallback: field names from schemas/bubble/Resources.csv (as of export)
_FALLBACK_FIELDS = [
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

_SCHEMA_PATH = Path(__file__).parent / "schemas" / "bubble" / "Resources.csv"

BUBBLE_RESOURCE_FIELDS: list[str] = load_bubble_resource_fields(_SCHEMA_PATH) or _FALLBACK_FIELDS
