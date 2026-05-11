# Bubble Object Schemas

> Auto-generated from live Bubble Data API on 2026-05-06.
> App: `art.bridgewayanalytics.com`

---

## Resource

**API type name:** `Resource`
**Total objects:** 1,019

Documents, guidelines, publications, and materials tracked in the knowledge base.

| Field | Type | Notes |
|-------|------|-------|
| `_id` | string | Bubble unique ID (read-only) |
| `Created Date` | string (ISO 8601) | Read-only |
| `Modified Date` | string (ISO 8601) | Read-only |
| `Created By` | string | User ID (read-only) |
| `Name` | string | Display name of the resource |
| `name-for-search` | string | Lowercase version of Name (auto-generated) |
| `URL` | string | Source URL |
| `date` | string (ISO 8601) | Publication / effective date |
| `Date display` | string | How date renders: `"Full date"`, `"Month, Year"`, `"Year"` |
| `full date display?` | string | `"Yes"` or `"No"` |
| `notes` | string | Rich text (Bubble BBCode-style formatting) |
| `Notes on chronicles` | boolean | Whether chronicle notes exist |
| `reference` | string | Short reference label (e.g. "Attachment 1") |
| `archive` | boolean | Archived flag |
| `internal` | boolean | Internal-only flag |
| `Order` | number | Sort order |
| `file` | string | Bubble CDN file URL |
| `file name` | string | Original filename |
| `Available To Vector Store` | boolean | Whether indexed for RAG search |
| `Organization` | list\<string\> | List of Tree Node IDs → **Organization** tree |
| `Type1` | list\<string\> | List of Tree Node IDs → **Resources Types** tree |
| `topics` | list\<string\> | List of Tree Node IDs → **Chronicles** tree |
| `parent` | string | Resource ID (self-referential hierarchy) |

**Fields in code (`schemas.py`) but not seen in API sample** (may be sparse/unused):
- `Chunk Overlap` (number) — RAG chunking parameter
- `Chunk Size` (number) — RAG chunking parameter
- `Domain` (string)
- `favorited` (boolean)
- `file2` (string) — secondary file
- `Related calendar items` (list\<string\>) — Calendar Item IDs
- `Suggestion` (string)
- `topic suggestion` (string)
- `Type` (string) — Tree Node ID
- `VS Content Type` (string)
- `Creator` (string) — User ID
- `Slug` (string)

---

## Calendar Item

**API type name:** `Calendar item`
**Total objects:** 620

Meetings, calls, and scheduled events on the NAIC calendar.

| Field | Type | Notes |
|-------|------|-------|
| `_id` | string | Bubble unique ID (read-only) |
| `Created Date` | string (ISO 8601) | Read-only |
| `Modified Date` | string (ISO 8601) | Read-only |
| `Created By` | string | User ID (read-only) |
| `title` | string | Event/meeting title |
| `date` | string (ISO 8601) | Start date/time |
| `End time` | string (ISO 8601) | End date/time |
| `length` | string | Duration (e.g. `"0:30"`, `"1:00"`) |
| `full day` | boolean | Full-day event flag |
| `event description` | string | Free-text description |
| `location` | string | Physical/virtual location |
| `subtopic` | string | Subtopic label |
| `color` | string | Hex color for calendar display (e.g. `"#00c672"`) |
| `Timezone Code` | string | IANA timezone (e.g. `"America/New_York"`) |
| `has topic` | boolean | Whether topic is assigned |
| `no agenda type` | string | Agenda type override |
| `Agenda` | list\<string\> | List of Resource IDs (agenda documents) |
| `alerts` | list\<string\> | List of Alert IDs |
| `topics` | list\<string\> | List of Tree Node IDs → **Chronicles** tree |
| `NAIC Group (tree node)` | string | Tree Node ID → **Organization** tree |
| `NAIC Group (legacy)` | string | Legacy tree node reference |
| `NAIC Date/Meeting Type` | string | Meeting type reference ID |
| `Outlook Event UID` | string | Outlook calendar sync UID |
| `Outlook last sync` | string (ISO 8601) | Last Outlook sync timestamp |
| `outlook_icaluid` | string | iCal UID for Outlook |

**Fields in code (`schemas.py`) but not seen in API sample:**
- `attached agenda items` (list\<string\>) — Agenda Item IDs
- `phone_number_and_ac…` (string) — Call-in number & access code
- `Relevant Documents` (list\<string\>) — Chronicle Link IDs

---

## Agenda Item

**API type name:** `Agenda item`
**Total objects:** 243

Individual agenda items discussed at NAIC working group meetings.

| Field | Type | Notes |
|-------|------|-------|
| `_id` | string | Bubble unique ID (read-only) |
| `Created Date` | string (ISO 8601) | Read-only |
| `Modified Date` | string (ISO 8601) | Read-only |
| `Created By` | string | User ID (read-only) |
| `NAIC Title` | string | Official NAIC title |
| `BA title` | string | Bridgeway Analytics internal title |
| `Ref #` | string | NAIC reference number (e.g. `"2024-14EP"`) |
| `BA Ref #` | string | BA-prefixed ref (e.g. `"SAPWG#2024-14EP"`) |
| `Category` | string | Category (e.g. `"SAP Clarification"`) |
| `Description` | string | Free-text description |
| `Status` | string | Current status (e.g. `"Spring 2024 NM - Exposed agenda item."`) |
| `Priority` | string | Priority level (e.g. `"A"`) |
| `Date Added` | string (ISO 8601) | When added to agenda |
| `Proposed By` | list\<string\> | List of proposer names (strings, not IDs) |
| `Proposed By- type` | list\<string\> | Proposer type labels |
| `Discussed at` | string | Tree Node ID → **Organization** tree (single) |
| `Discussed at list` | list\<string\> | Tree Node IDs → **Organization** tree (multiple) |
| `Topics` | list\<string\> | Tree Node IDs → **Chronicles** tree |
| `topics-dt` | list\<string\> | Tree Node IDs (duplicate/display variant) |
| `Resources` | list\<string\> | List of Resource IDs (related documents) |
| `Relevant Agenda Items` | list\<string\> | List of Agenda Item IDs (cross-references) |
| `SSAP Ref.` | list\<string\> | SSAP reference IDs |
| `SSAP Ref. - texts` | list\<string\> | SSAP reference display texts (e.g. `["Various"]`) |
| `text field for search` | string | Lowercase search text (auto-generated) |

---

## Alert

**API type name:** `Alert`
**Total objects:** 34

Alerts pushed from web-change-tracker pipeline to Bubble.

| Field | Type | Notes |
|-------|------|-------|
| `_id` | string | Bubble unique ID (read-only) |
| `Created Date` | string (ISO 8601) | Read-only |
| `Modified Date` | string (ISO 8601) | Read-only |
| `Created By` | string | User/system ID (read-only) |
| `Alert type` | string | Alert classification (e.g. `"Agenda Posted"`) |
| `date` | string (ISO 8601) | Alert date |

---

## Tree

**API type name:** `Tree`
**Total objects:** 16

Hierarchical taxonomy trees used for classification throughout the app.

| Field | Type | Notes |
|-------|------|-------|
| `_id` | string | Bubble unique ID (read-only) |
| `name` | string | Tree name |
| `Slug` | string | URL slug |
| `L1 nodes` | list\<string\> | Top-level Tree Node IDs |
| `column_filter` | string | Display label for filtering |
| `main organization tree` | boolean | Whether this is the primary org tree |
| `current editor` | string | User ID of current editor |
| `last edit` | string (ISO 8601) | Last edit timestamp |
| `per_state_filter` | boolean | State-level filtering enabled |

### Key Trees

| Tree Name | ID | Slug | Used For |
|-----------|----|----- |----------|
| Organization | `1709116928076x931948991473254400` | `organization` | `Organization` field on Resources, `NAIC Group (tree node)` on Calendar Items, `Discussed at` on Agenda Items |
| Resources Types | `1709123323342x151738792453079040` | `resource` | `Type1` field on Resources |
| Chronicles | `1710771208905x698956612053237800` | `chronicles` | `topics` on Resources/Calendar Items, `Topics` on Agenda Items |
| Newsreels | `1687747217110x798556091271610400` | `newsreels` | Newsreel article classification |

---

## Tree Node

**API type name:** `Tree node`
**Total objects:** 1,779

Individual nodes within taxonomy trees.

| Field | Type | Notes |
|-------|------|-------|
| `_id` | string | Bubble unique ID (read-only) |
| `Created Date` | string (ISO 8601) | Read-only |
| `Modified Date` | string (ISO 8601) | Read-only |
| `Created By` | string | User ID (read-only) |
| `name` | string | Full display name |
| `short name` | string | Abbreviated name |
| `level` | number | Depth in tree (1 = top-level) |
| `parent_node` | string | Parent Tree Node ID |
| `parent_tree` | string | Tree ID this node belongs to |
| `children` | list\<string\> | Child Tree Node IDs |
| `heritage (all parents)` | list\<string\> | All ancestor Tree Node IDs |
| `column_filter` | string | Display column label |
| `enabled_in_search` | boolean | Whether searchable |
| `isExample` | boolean | Example node flag |
| `filter_text` | list\<string\> | Search filter text variants |
| `duplicate_of` | string | Tree Node ID (if duplicated) |
| `latest_duplicate` | string | Most recent duplicate node ID |
| `special sort` | number | Custom sort order |
| `more_info` | string | Additional description |
| `self_connected_guidelines` | number | Count of directly connected guidelines |
| `children_connected_guidelines` | number | Count of guidelines connected via children |

---

## ID Reference Summary

Many fields store Bubble IDs that reference other object types:

| Field | Found On | References |
|-------|----------|------------|
| `Organization` | Resource | Tree Node IDs (Organization tree) |
| `Type1` | Resource | Tree Node IDs (Resources Types tree) |
| `topics` | Resource, Calendar Item | Tree Node IDs (Chronicles tree) |
| `Topics` | Agenda Item | Tree Node IDs (Chronicles tree) |
| `topics-dt` | Agenda Item | Tree Node IDs (Chronicles tree) |
| `parent` | Resource | Resource ID |
| `Related calendar items` | Resource | Calendar Item IDs |
| `Agenda` | Calendar Item | Resource IDs |
| `alerts` | Calendar Item | Alert IDs |
| `attached agenda items` | Calendar Item | Agenda Item IDs |
| `NAIC Group (tree node)` | Calendar Item | Tree Node ID (Organization tree) |
| `NAIC Group (legacy)` | Calendar Item | Tree Node ID (legacy) |
| `NAIC Date/Meeting Type` | Calendar Item | Reference ID |
| `Discussed at` | Agenda Item | Tree Node ID (Organization tree) |
| `Discussed at list` | Agenda Item | Tree Node IDs (Organization tree) |
| `Resources` | Agenda Item | Resource IDs |
| `Relevant Agenda Items` | Agenda Item | Agenda Item IDs |
| `SSAP Ref.` | Agenda Item | SSAP reference IDs |
| `parent_node` | Tree Node | Tree Node ID |
| `parent_tree` | Tree Node | Tree ID |
| `children` | Tree Node | Tree Node IDs |
| `L1 nodes` | Tree | Tree Node IDs |

---

## Notes

1. **No "Event" type exists** in Bubble currently. Meetings/events use the `Calendar item` type.
2. The Bubble API only returns fields that have non-null values. Fields listed as "not seen in API sample" may exist but were null across all sampled objects.
3. The `write allowlist` in `bubble/client.py` currently only permits `("Alert", "create")` and `("Calendar Item", "patch_alerts")`. The approval gate will need to expand this.
4. Field names are case-sensitive and must match exactly when writing to the Bubble API.
