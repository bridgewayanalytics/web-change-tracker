"""
Bridgemind (new Bubble app) — API constants and space/type reference.

Credentials are stored here for convenience and can be overridden by env vars
BUBBLE_API_URL and BUBBLE_API_KEY (which bubble/client.py reads first).

App: bridgemind  (eidarix.bridgewayanalytics.com)
Replaces: art.bridgewayanalytics.com (old app, deprecated)
"""

# ------------------------------------------------------------------
# API credentials (new app)
# ------------------------------------------------------------------
BUBBLE_API_URL = "https://eidarix.bridgewayanalytics.com/api/1.1/obj"
BUBBLE_API_KEY = "0a951ec86c08a59e274411913ce6aec3"

# ------------------------------------------------------------------
# Space ID — all data-scoped queries use this constraint
# ------------------------------------------------------------------
SPACE_ID = "1768998437948x865417918648382000"

SPACE_CONSTRAINT = [{"key": "space", "constraint_type": "equals", "value": SPACE_ID}]

# ------------------------------------------------------------------
# API type names (as used in GET /api/1.1/obj/<type>)
# These are the canonical endpoint names; display names differ (see docs/bubble_data_model.md)
# ------------------------------------------------------------------
TYPE_CALENDAR_ITEM  = "calendaritem"   # display: "Calendar Item"  — meetings, RFCs, effective dates
TYPE_LIBRARY_ITEM   = "libraryitem"    # display: "Library item"   — agendas, materials, documents
TYPE_ORGANIZATION   = "organization"   # display: "Organization"   — org hierarchy tree
TYPE_CHRONICLE      = "chronicle"      # display: "Chronicle"      — top-level chronicle categories
TYPE_CHRONICLE_TOPIC = "chronicletopic" # display: "Chronicle Topic" — topic taxonomy nodes
TYPE_ISSUE          = "issue"          # display: "Issue"          — weekly newsreel editions (also accessible as "newsreel")
TYPE_CONTENT_BIT    = "contentbit"     # display: "Content bit"    — newsreel content blocks
TYPE_PARAGRAPH      = "paragraph"      # display: "Paragraph"      — sub-blocks within content bits

# Types referenced in schemas but NOT exposed via GET API:
#   agendaitem   (also called subtopic in field IDs) — linked to calendaritem and libraryitem
#   calendaritemtype — calendar item type lookup (Meeting, RFC, Adopted Guideline, etc.)
#   libraryitemtype  — library item type lookup (Agenda, Materials, RFC, etc.)
#   treenode, chroniclelink, sourcematerial, resourcesuggestion, newsitem, reminder, jurisdiction


def get_client(use_cache: bool = False):
    """Return a BubbleClient configured for the bridgemind app."""
    from bubble.client import BubbleClient
    return BubbleClient(base_url=BUBBLE_API_URL, api_key=BUBBLE_API_KEY, use_cache=use_cache)
