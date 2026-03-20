# Vectorization Model Specification

> Handoff document for a new repository that transforms Bubble.io objects into a normalized, vectorization-ready data model for downstream retrieval, enrichment, and AI workflows.

---

## 1. Objective

Bubble.io serves as the primary data store for Bridgeway's insurance regulatory tracking system. The data model is optimized for Bubble's no-code UI — not for querying, joining, or AI retrieval.

This new project will:

1. **Extract** all relevant objects from the Bubble Data API
2. **Normalize** them into a clean relational schema with proper foreign keys, human-readable names alongside IDs, and consistent types
3. **Produce** flat, joinable tables (parquet/jsonl) plus embedding-ready text views for each entity type
4. **Enable** downstream vector search, RAG pipelines, and structured analytics that are impractical against the raw Bubble API

The output should be a standalone ETL pipeline that can run on a schedule, producing versioned snapshots of the normalized data.

---

## 2. Current Bubble Object Model

### 2.1 Major Object Types

| Bubble Type | API Name | Approx Count | Role |
|-------------|----------|-------------|------|
| Resource | `Resource` | ~2,000+ | PDFs, documents, publications, agenda materials |
| Calendar Item | `Calendar item` | ~500+ | Meetings, calls, events |
| Agenda Item | `Agenda item` | ~231 | Regulatory work items tracked across meetings |
| Tree Node | `Tree node` | ~1,600+ (across all trees) | Hierarchical classification nodes |
| Tree | `Tree` | 16 | Container for tree node hierarchies |
| Alert | `Alert` | ~100+ | Notifications about new resources/materials |

### 2.2 Key Trees (Classification Hierarchies)

| Tree Name | Node Count | Purpose |
|-----------|-----------|---------|
| **Organization** | 140 | NAIC committee/group hierarchy (e.g., NAIC > Financial Condition (E) Committee > Capital Adequacy (E) Task Force) |
| **Chronicles** | 87 | Topic/subject classification (e.g., "NAIC Investments" > "Collateral Loans", "CMBS & RMBS") |
| **Resources Types** | 11 | Document type classification (Publication, Agenda & Materials, In the Weeds, etc.) |
| Asset Classes | 381 | Investment asset taxonomy (not used in this project) |
| Insurance Entity Type | 50 | Entity type taxonomy (not used in this project) |

### 2.3 Resource (Library Item)

The primary document entity — represents a PDF, webpage, or other tracked content.

| Field | Type | Description | Notes |
|-------|------|-------------|-------|
| `_id` | string | Bubble object ID (PK) | Format: `1731019820042x652190297443795000` |
| `Name` | string | Human-readable title | e.g., "Climate and Resiliency (EX) Task Force - November 19, 2024 - Meeting Agenda" |
| `URL` | string | Link to the document | Can be NAIC PDF, external report, etc. |
| `date` | ISO datetime | Publication/detection date | Sometimes null; may be inferred from PDF metadata |
| `Date display` | string | Display format hint | "Full date", "Month/Year", etc. |
| `notes` | string | Description/notes | Often auto-generated: "New docs in {group}" |
| `Domain` | string | Source domain | e.g., "content.naic.org" |
| `Organization` | list of Tree Node IDs | NAIC org path | Resolved to Organization tree nodes |
| `Type1` | list of Tree Node IDs | Document type | Points to Resources Types tree (Publication, Agenda & Materials, etc.) |
| `topic suggestion` | Tree Node ID (string) | AI-suggested Chronicle topic | Single value — points to Chronicles tree |
| `Related calendar items` | list of Calendar Item IDs | Linked meetings | Which calendar events this document relates to |
| `parent` | Resource ID | Parent resource | Self-referential hierarchy |
| `file` | file | Attached file | Bubble file storage |
| `file name` | string | File name | |
| `archive` | boolean | Archived flag | |
| `internal` | boolean | Internal-only flag | |
| `Available To Vector Store` | boolean | AI indexing flag | |
| `Chunk Size` | number | PDF chunk size for vectorization | |
| `Chunk Overlap` | number | Chunk overlap for vectorization | |
| `VS Content Type` | string | Vector store content type | "PDF" |
| `name-for-search` | string | Normalized lowercase title | Used for Bubble full-text search |
| `Slug` | string | URL-friendly identifier | |
| `Created Date` | ISO datetime | Creation timestamp | |
| `Modified Date` | ISO datetime | Last modification | |

### 2.4 Calendar Item

Represents a meeting, call, or scheduled event.

| Field | Type | Description | Notes |
|-------|------|-------------|-------|
| `_id` | string | Bubble object ID (PK) | |
| `title` | string | Event title | Format often: "NAIC {Group} \| {Topic}" or "{Group} Meeting" |
| `date` | ISO datetime | Start date/time | |
| `End time` | ISO datetime | End date/time | |
| `Timezone Code` | string | Timezone | Default: "America/New_York" |
| `location` | string | Physical/virtual location | |
| `event description` | string | Notes/description | |
| `full day` | boolean | All-day event flag | |
| `length` | string | Duration | e.g., "0:30", "2:00" |
| `color` | string | UI display color | e.g., "#00c672" |
| `NAIC Group (tree node)` | Tree Node ID (string) | NAIC group | Points to Organization tree |
| `NAIC Group (legacy)` | string | Legacy group reference | Deprecated — was a different ID format |
| `NAIC Date/Meeting Type` | string | Meeting type classification | Bubble internal reference |
| `subtopic` | Tree Node ID or string | Sub-topic | Sometimes a string label, sometimes a node ID |
| `has topic` | boolean | Whether topics are assigned | |
| `Agenda` | list of dicts | Agenda/materials links | Format: `[{"url": "...", "title": "..."}]` |
| `attached agenda items` | list of Agenda Item IDs | Linked agenda items | |
| `Relevant Documents` | list | Chronicle document links | |
| `alerts` | list of Alert objects | Attached alerts | Inline alert objects (not IDs) |
| `Outlook Event UID` | string | Outlook sync ID | |
| `outlook_icaluid` | string | iCal UID | |
| `Outlook last sync` | ISO datetime | Last Outlook sync | |

### 2.5 Agenda Item

A regulatory work item tracked across meetings — represents a specific proposal, exposure, or initiative.

| Field | Type | Description | Notes |
|-------|------|-------------|-------|
| `_id` | string | Bubble object ID (PK) | |
| `BA title` | string | Primary title | e.g., "ASU 2023-09, Improvements To Income Tax Disclosures" |
| `NAIC Title` | string | Alternative NAIC title | Sometimes differs from BA title |
| `BA Ref #` | string | Reference number with group prefix | e.g., "SAPWG#2024-11", "RBC-IRE-WG#2025-22" |
| `Ref #` | string | Bare reference number | e.g., "2024-11" (no group prefix) |
| `Description` | string | Full description | Can be multi-paragraph |
| `Category` | string | Item category | e.g., "SAP Clarification", "New SSAP" |
| `Status` | string | Processing status | Free text, often multi-line |
| `Priority` | string | Priority level | e.g., "A", "B" |
| `Date Added` | ISO datetime | When item was added | |
| `Proposed By` | list of strings | Who proposed this | e.g., ["NAIC Staff"] |
| `Topics` | list of Tree Node IDs | Chronicle topics | Points to Chronicles tree. **175 of 231 items have this populated.** |
| `Resources` | list of Resource IDs | Linked documents | **163 of 231 items have this populated.** |
| `Discussed at list` | list of Tree Node IDs | NAIC groups where discussed | Points to Organization tree. **~153 of 231 items have this populated; 78 are empty.** |
| `Discussed at` | Tree Node ID (string) | Legacy single-group field | Older format; `Discussed at list` is preferred |
| `SSAP Ref.` | list of IDs | SSAP reference links | Statutory accounting standards |
| `SSAP Ref. - texts` | list of strings | Human-readable SSAP refs | e.g., ["101", "Various"] |
| `text field for search` | string | Concatenated searchable text | Auto-generated from title + ref + SSAP |

### 2.6 Alert

Notification generated when new content is detected for a tracked calendar item.

| Field | Type | Description | Notes |
|-------|------|-------------|-------|
| `_id` | string | Bubble object ID (PK) | |
| `Alert type` | string | Alert classification | One of: "Agenda Posted", "Materials Posted", "Meeting Link Posted", "New Resource" |
| `date` | ISO datetime | Alert creation date | |
| `Related calendar item` | Calendar Item ID (string) | Which meeting this alert is about | |
| `Trigger URL` | string | URL of the resource that triggered the alert | Sometimes missing on older alerts |

### 2.7 Tree Node (Chronicles / Organization / Resources Types)

Hierarchical classification node used across all trees.

| Field | Type | Description | Notes |
|-------|------|-------------|-------|
| `_id` | string | Bubble object ID (PK) | |
| `name` / `Name` | string | Display name | Casing inconsistent between live API (lowercase) and exports |
| `parent_node` / `parent` / `Parent` | Tree Node ID or dict | Parent node reference | Three possible field names; can be string ID or `{"_id": "..."}` dict |
| `parent_tree` / `Tree` / `tree` | Tree ID or dict | Which tree this belongs to | |
| `level` | number | Depth in hierarchy | 0 = root, 1 = first child level, etc. |
| `children` | list of Tree Node IDs | Child nodes | |
| `chronicle` | string | Chronicle/topic name | Only on Chronicles tree nodes |
| `short name` | string | Abbreviated name | |
| `enabled_in_search` | boolean | Searchable flag | |
| `special sort` | number | Custom sort order | |

### 2.8 Known Inconsistencies and Constraints

1. **Field name casing**: Live API returns lowercase (`name`, `date`); some exports/schemas use capitalized (`Name`, `Date`). Code handles both with fallback: `obj.get("name") or obj.get("Name")`.

2. **Parent references are polymorphic**: Can be a bare string ID or a nested dict `{"_id": "...", "name": "..."}`. Must handle both.

3. **List fields sometimes return strings**: A field documented as "list" may return a single string when there's only one value. Must normalize: `[x] if isinstance(x, str) else x`.

4. **`Discussed at` vs `Discussed at list`**: Legacy single-value field vs modern list field. 78 of 231 agenda items have empty `Discussed at list`, making them invisible to group-scoped queries. These items can sometimes be recovered by parsing the `BA Ref #` prefix (e.g., "SAPWG#2024-11" implies SAPWG group).

5. **`BA Ref #` is not globally unique**: "2024-26" under CATF is different from "2024-26" under SAPWG. The group prefix (e.g., "SAPWG#", "CATF#") disambiguates, but some refs lack prefixes.

6. **Multi-ref fields**: A single `BA Ref #` field can contain multiple refs: "SAPWG#2019-21 and LRBCWG#2024-L8". Requires regex parsing.

7. **Null vs empty**: Fields may be `null`, `""`, or `[]` — all meaning "not set". Defensive handling required.

8. **Calendar Item `Agenda` field**: Contains inline dicts `[{"url": "...", "title": "..."}]`, not references to Resource objects. These are meeting materials discovered by the scraper, not necessarily matching any Resource record.

9. **Alert embedding**: Alerts are embedded in Calendar Item's `alerts` list field (inline objects), but also exist as standalone Alert objects in Bubble. The Calendar Item field and the standalone Alert may not always be in sync.

10. **`topic suggestion` is single-valued**: Despite resources potentially spanning multiple topics, the Bubble field only stores one topic node ID.

---

## 3. Proposed Normalized Model

### 3.1 Design Principles

- Every entity gets a stable surrogate key (`id`) that maps 1:1 to the Bubble `_id`
- Human-readable names are always stored alongside IDs (denormalized for usability)
- Multi-value relationships use explicit join tables
- Fields needed for embedding are in the entity table; fields only needed for structured queries stay separate
- Dates are ISO 8601 strings; booleans are true/false; nulls are explicit

### 3.2 Primary Entity Tables

#### `calendar_items`

Meetings, calls, and scheduled events.

| Column | Type | Source | Notes |
|--------|------|--------|-------|
| `id` | string (PK) | `_id` | Bubble ID |
| `title` | string | `title` | |
| `date` | datetime | `date` | Start time |
| `end_time` | datetime | `End time` | |
| `timezone` | string | `Timezone Code` | Default: "America/New_York" |
| `location` | string | `location` | |
| `description` | string | `event description` | |
| `is_full_day` | boolean | `full day` | |
| `duration` | string | `length` | e.g., "0:30" |
| `naic_group_id` | string (FK → org_nodes.id) | `NAIC Group (tree node)` | |
| `naic_group_name` | string | Resolved from tree node | Denormalized for convenience |
| `naic_group_path` | string | Resolved from tree hierarchy | e.g., "NAIC > Financial Condition (E) Committee > SAPWG" |
| `subtopic` | string | `subtopic` | Resolved to name if tree node ID |
| `has_topic` | boolean | `has topic` | |
| `created_at` | datetime | `Created Date` | |
| `modified_at` | datetime | `Modified Date` | |

#### `resources`

Documents, PDFs, publications, agenda materials.

| Column | Type | Source | Notes |
|--------|------|--------|-------|
| `id` | string (PK) | `_id` | Bubble ID |
| `name` | string | `Name` | Document title |
| `url` | string | `URL` | |
| `date` | datetime | `date` | Publication date |
| `date_display` | string | `Date display` | |
| `notes` | string | `notes` | |
| `domain` | string | `Domain` | Source domain |
| `resource_type` | string | Resolved from `Type1[0]` | e.g., "Publication", "Agenda & Materials" |
| `resource_type_id` | string (FK → resource_types.id) | `Type1[0]` | |
| `topic_suggestion_id` | string (FK → chronicle_topics.id) | `topic suggestion` | |
| `topic_suggestion_name` | string | Resolved from tree node | Denormalized |
| `is_archived` | boolean | `archive` | |
| `is_internal` | boolean | `internal` | |
| `vectorizable` | boolean | `Available To Vector Store` | |
| `parent_resource_id` | string (FK → resources.id) | `parent` | |
| `created_at` | datetime | `Created Date` | |
| `modified_at` | datetime | `Modified Date` | |

#### `agenda_items`

Regulatory work items tracked across meetings.

| Column | Type | Source | Notes |
|--------|------|--------|-------|
| `id` | string (PK) | `_id` | Bubble ID |
| `ba_title` | string | `BA title` | Primary title |
| `naic_title` | string | `NAIC Title` | Alternative title |
| `ba_ref` | string | `BA Ref #` | Full ref with prefix: "SAPWG#2024-11" |
| `ref_number` | string | `Ref #` | Bare ref: "2024-11" |
| `ref_prefix` | string | Parsed from `BA Ref #` | Group prefix: "SAPWG", "CATF", etc. |
| `description` | string | `Description` | |
| `category` | string | `Category` | |
| `status` | string | `Status` | Cleaned (trimmed whitespace/newlines) |
| `priority` | string | `Priority` | |
| `date_added` | datetime | `Date Added` | |
| `proposed_by` | string | `Proposed By` joined | e.g., "NAIC Staff" |
| `ssap_refs` | string | `SSAP Ref. - texts` joined | e.g., "101, Various" |
| `primary_group_id` | string (FK → org_nodes.id) | `Discussed at list[0]` or `Discussed at` | Primary discussing group |
| `primary_group_name` | string | Resolved from tree node | |
| `created_at` | datetime | `Created Date` | |
| `modified_at` | datetime | `Modified Date` | |

#### `chronicle_topics`

Topic classification nodes from the Chronicles tree.

| Column | Type | Source | Notes |
|--------|------|--------|-------|
| `id` | string (PK) | `_id` | Bubble tree node ID |
| `name` | string | `name` or `chronicle` | Topic display name |
| `parent_id` | string (FK → chronicle_topics.id) | `parent_node` | |
| `parent_name` | string | Resolved | |
| `path` | string | Computed | e.g., "Chronicles > NAIC Investments > Collateral Loans" |
| `level` | integer | `level` | Depth in hierarchy |
| `sort_order` | integer | `special sort` | |

#### `org_nodes`

Organization hierarchy nodes (NAIC committees, working groups, task forces).

| Column | Type | Source | Notes |
|--------|------|--------|-------|
| `id` | string (PK) | `_id` | Bubble tree node ID |
| `name` | string | `name` | Node display name |
| `parent_id` | string (FK → org_nodes.id) | `parent_node` | |
| `path` | string | Computed | e.g., "NAIC > Financial Condition (E) Committee > SAPWG" |
| `level` | integer | `level` | |

#### `resource_types`

Resource type classification from the Resources Types tree.

| Column | Type | Source | Notes |
|--------|------|--------|-------|
| `id` | string (PK) | `_id` | |
| `name` | string | `name` | e.g., "Publication", "Agenda & Materials" |

#### `alerts`

Notifications about new content.

| Column | Type | Source | Notes |
|--------|------|--------|-------|
| `id` | string (PK) | `_id` | Bubble ID |
| `alert_type` | string | `Alert type` | "Agenda Posted", "Materials Posted", etc. |
| `date` | datetime | `date` | |
| `calendar_item_id` | string (FK → calendar_items.id) | `Related calendar item` | |
| `trigger_url` | string | `Trigger URL` | |
| `created_at` | datetime | `Created Date` | |

### 3.3 Join Tables

#### `resource_calendar_items`

Links resources to their related calendar items (many-to-many).

| Column | Type | Source |
|--------|------|--------|
| `resource_id` | string (FK → resources.id) | Resource `_id` |
| `calendar_item_id` | string (FK → calendar_items.id) | From `Related calendar items` list |

#### `resource_organizations`

Links resources to organization nodes (many-to-many).

| Column | Type | Source |
|--------|------|--------|
| `resource_id` | string (FK → resources.id) | Resource `_id` |
| `org_node_id` | string (FK → org_nodes.id) | From `Organization` list |
| `org_node_name` | string | Resolved name |

#### `agenda_item_topics`

Links agenda items to Chronicle topics (many-to-many).

| Column | Type | Source |
|--------|------|--------|
| `agenda_item_id` | string (FK → agenda_items.id) | Agenda Item `_id` |
| `topic_id` | string (FK → chronicle_topics.id) | From `Topics` list |
| `topic_name` | string | Resolved name |

#### `agenda_item_resources`

Links agenda items to resources (many-to-many).

| Column | Type | Source |
|--------|------|--------|
| `agenda_item_id` | string (FK → agenda_items.id) | Agenda Item `_id` |
| `resource_id` | string (FK → resources.id) | From `Resources` list |

#### `agenda_item_groups`

Links agenda items to NAIC groups where they were discussed (many-to-many).

| Column | Type | Source |
|--------|------|--------|
| `agenda_item_id` | string (FK → agenda_items.id) | Agenda Item `_id` |
| `org_node_id` | string (FK → org_nodes.id) | From `Discussed at list` |
| `org_node_name` | string | Resolved name |

#### `calendar_item_agenda_items`

Links calendar items to their attached agenda items (many-to-many).

| Column | Type | Source |
|--------|------|--------|
| `calendar_item_id` | string (FK → calendar_items.id) | Calendar Item `_id` |
| `agenda_item_id` | string (FK → agenda_items.id) | From `attached agenda items` list |

#### `calendar_item_materials`

Inline agenda/materials links on calendar items (not necessarily matching a Resource record).

| Column | Type | Source |
|--------|------|--------|
| `calendar_item_id` | string (FK → calendar_items.id) | Calendar Item `_id` |
| `url` | string | From `Agenda[].url` |
| `title` | string | From `Agenda[].title` |

### 3.4 Derived Entity Views

These are not separate Bubble types — they are **views** derived from the Resource table using `Type1` classification.

#### Reports View

Resources where `Type1` is "Publication" and the content is an analytical report (Capital Markets Bureau reports, IAIS reports, FSB reports, etc.).

```sql
SELECT r.*, rt.name as resource_type
FROM resources r
JOIN resource_types rt ON r.resource_type_id = rt.id
WHERE rt.name = 'Publication'
```

#### Requests for Comment View

Resources where `Type1` is "Proposed Guidance & Support Materials" — exposed proposals, comment letters, draft guidance.

```sql
SELECT r.*, rt.name as resource_type
FROM resources r
JOIN resource_types rt ON r.resource_type_id = rt.id
WHERE rt.name = 'Proposed Guidance & Support Materials'
```

#### Agenda & Meeting Materials View

Resources where `Type1` is "Agenda & Materials" — meeting agendas, minutes, presentations, handouts.

```sql
SELECT r.*, rt.name as resource_type
FROM resources r
JOIN resource_types rt ON r.resource_type_id = rt.id
WHERE rt.name = 'Agenda & Materials'
```

> **Note**: "Report" and "Request for Comment" are not distinct Bubble types. They are filtered views of the Resource table. The `Type1` field and the `Category` field on linked Agenda Items provide the signal for classification. The new system should preserve this — use the `resource_type` column for filtering, not separate tables.

---

## 4. Mapping Rules: Bubble to Normalized Model

### 4.1 Resource Mapping

```
Bubble Resource → resources table
  _id → id
  Name → name
  URL → url
  date → date (parse ISO, handle null)
  Date display → date_display
  notes → notes
  Domain → domain
  Type1[0] → resource_type_id (resolve node ID → name for resource_type)
  topic suggestion → topic_suggestion_id (resolve node ID → name)
  archive → is_archived (normalize to boolean)
  internal → is_internal (normalize to boolean)
  Available To Vector Store → vectorizable
  parent → parent_resource_id
  Created Date → created_at
  Modified Date → modified_at

Bubble Resource.Organization → resource_organizations join table
  For each org_id in Organization list:
    resource_id = _id, org_node_id = org_id, org_node_name = resolved

Bubble Resource.Related calendar items → resource_calendar_items join table
  For each cal_id in Related calendar items list:
    resource_id = _id, calendar_item_id = cal_id
```

**Transformations:**
- `Type1` is a list but usually single-valued; take first element
- Boolean fields may be `true`/`false`/`"yes"`/`"no"`/`null` — normalize to boolean
- `topic suggestion` may be null (29 of 30 resources in our eval had one)
- `Organization` list may be empty — create no join rows

### 4.2 Calendar Item Mapping

```
Bubble Calendar Item → calendar_items table
  _id → id
  title → title
  date → date
  End time → end_time
  Timezone Code → timezone
  location → location
  event description → description
  full day → is_full_day (normalize boolean)
  length → duration
  NAIC Group (tree node) → naic_group_id (resolve → naic_group_name, naic_group_path)
  subtopic → subtopic (resolve node ID to name if applicable)
  has topic → has_topic
  Created Date → created_at
  Modified Date → modified_at

Bubble Calendar Item.attached agenda items → calendar_item_agenda_items join
Bubble Calendar Item.Agenda[] → calendar_item_materials join
Bubble Calendar Item.alerts[] → alerts table (extract embedded objects)
```

**Transformations:**
- `NAIC Group (tree node)` is a single ID — resolve to full path by walking the Organization tree
- `Agenda` field contains inline dicts, not references — extract to `calendar_item_materials`
- `alerts` field contains embedded Alert objects — extract to `alerts` table, deduplicating against standalone Alert records by matching on `_id`
- `subtopic` may be a tree node ID or a string label — resolve if ID

### 4.3 Agenda Item Mapping

```
Bubble Agenda Item → agenda_items table
  _id → id
  BA title → ba_title
  NAIC Title → naic_title
  BA Ref # → ba_ref
  Ref # → ref_number
  (parsed from BA Ref #) → ref_prefix (e.g., "SAPWG", "CATF")
  Description → description
  Category → category
  Status → status (trim whitespace/newlines)
  Priority → priority
  Date Added → date_added
  Proposed By → proposed_by (join list with ", ")
  SSAP Ref. - texts → ssap_refs (join list with ", ")
  Discussed at list[0] or Discussed at → primary_group_id (resolve → primary_group_name)
  Created Date → created_at
  Modified Date → modified_at

Bubble Agenda Item.Topics → agenda_item_topics join
Bubble Agenda Item.Resources → agenda_item_resources join
Bubble Agenda Item.Discussed at list → agenda_item_groups join
```

**Transformations:**
- `ref_prefix` is parsed from `BA Ref #` using regex: `^(RBC-IRE-WG|RBC-IRE|SAPWG|CATF|LATF|BWG|LRBCWG|VOSTF|...)(?:[#\s\-_]|$)`
- `Status` often has trailing newlines — strip them
- `Proposed By` is a list of strings — join for flat storage
- `Discussed at list` vs `Discussed at`: prefer list field; fall back to single field if list is empty
- For the 78 items with empty `Discussed at list`: use `ref_prefix` to infer group if possible

### 4.4 Alert Mapping

```
Bubble Alert → alerts table
  _id → id
  Alert type → alert_type
  date → date
  Related calendar item → calendar_item_id
  Trigger URL → trigger_url
  Created Date → created_at
```

**Transformations:**
- Alerts exist both as standalone Bubble objects AND embedded in Calendar Item `alerts` fields. Deduplicate by `_id`.
- Some older alerts lack `Trigger URL` — accept null.

### 4.5 Tree Node Mapping

```
Chronicles tree nodes → chronicle_topics table
  _id → id
  name/Name/chronicle → name (use first non-empty)
  parent_node/parent/Parent → parent_id (extract string ID from dict if needed)
  (computed) → path (walk parent chain to root)
  level → level
  special sort → sort_order

Organization tree nodes → org_nodes table
  Same pattern as above

Resources Types tree nodes → resource_types table
  _id → id
  name/Name → name
```

**Transformations:**
- Parent field is polymorphic: can be string ID or `{"_id": "...", "name": "..."}` dict. Extract string ID.
- Compute `path` by walking the parent chain to root, joining with " > ".
- Filter out the root/container node (level 0, usually named same as tree).

### 4.6 Handling Multi-Value Relationships

All multi-value Bubble fields (lists of IDs) become join table rows:

| Bubble Field | Join Table | Left FK | Right FK |
|-------------|-----------|---------|----------|
| Resource.Organization | resource_organizations | resource_id | org_node_id |
| Resource.Related calendar items | resource_calendar_items | resource_id | calendar_item_id |
| Resource.Type1 | (denormalized on resource) | — | — |
| Agenda Item.Topics | agenda_item_topics | agenda_item_id | topic_id |
| Agenda Item.Resources | agenda_item_resources | agenda_item_id | resource_id |
| Agenda Item.Discussed at list | agenda_item_groups | agenda_item_id | org_node_id |
| Calendar Item.attached agenda items | calendar_item_agenda_items | calendar_item_id | agenda_item_id |
| Calendar Item.Agenda | calendar_item_materials | calendar_item_id | (url, title) |

### 4.7 Resolving Names vs IDs

Every tree node reference in Bubble is stored as an opaque ID (e.g., `"1710771343109x631718453703900000"`). The normalization pipeline must:

1. **Pre-fetch all tree nodes** for Organization, Chronicles, and Resources Types trees
2. **Build ID → name/path lookup dicts** in memory
3. **Resolve during mapping**: for each ID field, store both the ID (for joining) and the resolved name (for readability/embedding)
4. **Handle missing nodes gracefully**: if an ID doesn't resolve, store the raw ID and flag it in a validation report

---

## 5. Vectorization Guidance

### 5.1 Embedding Text Construction

For each entity type, concatenate specific fields into a single text block for embedding. The goal is to produce a natural-language summary that captures what the entity IS, what it's ABOUT, and how it CONNECTS.

#### Calendar Item Embedding Text

```
{title}
Date: {date}
Group: {naic_group_path}
Description: {description}
Agenda Items: {comma-separated ba_titles of linked agenda items}
Topics: {comma-separated topic names from linked agenda items}
Materials: {comma-separated titles from calendar_item_materials}
Alerts: {comma-separated alert_type values}
```

#### Resource Embedding Text

```
{name}
Type: {resource_type}
Date: {date}
URL: {url}
Topic: {topic_suggestion_name}
Organization: {org_node_names from resource_organizations}
Notes: {notes}
Related Agenda Items: {ba_titles from agenda_item_resources where resource_id = this}
Related Topics: {topic names via agenda items}
Related Meetings: {titles from resource_calendar_items}
```

#### Agenda Item Embedding Text

```
{ba_title}
Reference: {ba_ref}
Category: {category}
Status: {status}
Description: {description}
Group: {primary_group_name}
Topics: {comma-separated topic names from agenda_item_topics}
SSAP References: {ssap_refs}
Proposed By: {proposed_by}
Linked Resources: {count} documents
```

#### Chronicle Topic Embedding Text

```
{name}
Path: {path}
Parent Topic: {parent_name}
Agenda Items: {comma-separated ba_titles of items with this topic}
Resource Count: {count of resources with this topic suggestion}
```

### 5.2 Fields to Embed vs Keep Structured

**Embed (concatenate into text):**
- Titles, names, descriptions
- Group/organization names (human-readable)
- Topic names
- Status text
- Notes
- Alert type labels
- Related entity titles (denormalized)

**Keep structured (columns only — do NOT embed):**
- All IDs (Bubble IDs, foreign keys)
- Dates (store as ISO strings for filtering)
- Booleans (is_archived, is_internal, has_topic)
- Counts
- URLs (store for retrieval, not embedding)
- Duration, timezone, color
- Sort orders, levels

### 5.3 Embedding Strategy Recommendations

- Use a single embedding per entity row (not per-field)
- Recommended chunk size: most entities fit in a single embedding (~200-500 tokens)
- For Resources with long `notes` or Agenda Items with long `Description`, consider truncation to 2000 characters
- Store the embedding text alongside the embedding vector for debugging
- Use metadata filters (date ranges, resource_type, naic_group) to narrow retrieval before vector search

---

## 6. Entity Resolution and Joining Guidance

### 6.1 Agenda Items ↔ Chronicle Topics

This is the most important bridge for downstream AI workflows.

```
agenda_items
  → agenda_item_topics (join table)
    → chronicle_topics

175 of 231 agenda items have Topics populated.
56 agenda items have NO topics — these are data quality gaps in Bubble.
```

**Joining:**
```sql
SELECT ai.ba_title, ct.name as topic_name, ct.path as topic_path
FROM agenda_items ai
JOIN agenda_item_topics ait ON ai.id = ait.agenda_item_id
JOIN chronicle_topics ct ON ait.topic_id = ct.id
```

### 6.2 Resources ↔ Agenda Items

Resources connect to agenda items in TWO directions — capture both:

1. **Agenda Item → Resource** (forward): Agenda Item's `Resources` list contains Resource IDs
2. **Resource → Agenda Item** (reverse): Found via ref number matching, title matching, or AI enrichment

The `agenda_item_resources` join table captures direction 1 (the authoritative Bubble data). Direction 2 is the output of the enrichment pipeline in the web-change-tracker repo and should be treated as supplementary.

### 6.3 Resources ↔ Calendar Items

```
resources
  → resource_calendar_items (join table)
    → calendar_items
```

This link is established by the enrichment pipeline (not raw Bubble data for new resources). Existing resources may have this populated by analysts.

### 6.4 Calendar Items ↔ Agenda Items

Two paths:

1. **Calendar Item → Agenda Items**: via `attached agenda items` field → `calendar_item_agenda_items` join
2. **Agenda Items → Calendar Groups**: via `Discussed at list` → `agenda_item_groups` join, which connects to the same `org_nodes` that Calendar Items reference via `naic_group_id`

To find all agenda items for a meeting:
```sql
-- Direct link
SELECT ai.* FROM agenda_items ai
JOIN calendar_item_agenda_items ciai ON ai.id = ciai.agenda_item_id
WHERE ciai.calendar_item_id = ?

-- Via shared NAIC group
SELECT ai.* FROM agenda_items ai
JOIN agenda_item_groups aig ON ai.id = aig.agenda_item_id
JOIN calendar_items ci ON ci.naic_group_id = aig.org_node_id
WHERE ci.id = ?
```

### 6.5 Alerts ↔ Calendar Items ↔ Resources

```
alerts.calendar_item_id → calendar_items.id
alerts.trigger_url → resources.url (soft join)
```

Alerts bridge calendar items to the specific resource that triggered them. The `trigger_url` can be joined to `resources.url` for the full chain: Alert → Calendar Item → Resource.

### 6.6 Full Entity Graph

```
                    chronicle_topics
                         ↑
                   agenda_item_topics
                         ↑
resources ←→ agenda_item_resources ←→ agenda_items ←→ agenda_item_groups → org_nodes
    ↓                                       ↑                                  ↑
resource_calendar_items          calendar_item_agenda_items              calendar_items
    ↓                                       ↑                                  ↑
calendar_items ──────────────────────────────┘                              alerts
```

---

## 7. Recommended Output Artifacts

The new repository should produce the following outputs per run:

### 7.1 Normalized Tables (parquet + jsonl)

```
output/
├── tables/
│   ├── resources.parquet
│   ├── calendar_items.parquet
│   ├── agenda_items.parquet
│   ├── chronicle_topics.parquet
│   ├── org_nodes.parquet
│   ├── resource_types.parquet
│   ├── alerts.parquet
│   ├── resource_calendar_items.parquet
│   ├── resource_organizations.parquet
│   ├── agenda_item_topics.parquet
│   ├── agenda_item_resources.parquet
│   ├── agenda_item_groups.parquet
│   ├── calendar_item_agenda_items.parquet
│   └── calendar_item_materials.parquet
├── embeddings/
│   ├── resources_embedding_text.jsonl
│   ├── calendar_items_embedding_text.jsonl
│   ├── agenda_items_embedding_text.jsonl
│   └── chronicle_topics_embedding_text.jsonl
├── validation/
│   ├── validation_report.json
│   └── unresolved_ids.json
└── metadata/
    ├── run_manifest.json
    └── schema_version.json
```

### 7.2 Embedding-Ready Text Views (jsonl)

Each line is a JSON object:

```json
{
  "id": "1731019820042x652190297443795000",
  "entity_type": "resource",
  "text": "Climate and Resiliency (EX) Task Force - November 19, 2024 - Meeting Agenda\nType: Agenda & Materials\nDate: 2024-11-19\n...",
  "metadata": {
    "date": "2024-11-19",
    "resource_type": "Agenda & Materials",
    "naic_group": "Climate and Resiliency (EX) Task Force",
    "topic": "NAIC Climate Initiatives",
    "url": "https://content.naic.org/sites/default/files/..."
  }
}
```

### 7.3 Validation Reports

```json
{
  "run_timestamp": "2026-03-20T12:00:00Z",
  "entity_counts": {
    "resources": 2100,
    "calendar_items": 520,
    "agenda_items": 231,
    "chronicle_topics": 87,
    "org_nodes": 140,
    "alerts": 105
  },
  "join_coverage": {
    "resources_with_calendar_links": {"count": 450, "pct": 0.214},
    "resources_with_topic_suggestion": {"count": 1800, "pct": 0.857},
    "agenda_items_with_topics": {"count": 175, "pct": 0.757},
    "agenda_items_with_resources": {"count": 163, "pct": 0.706},
    "agenda_items_with_group": {"count": 153, "pct": 0.662}
  },
  "unresolved_ids": {
    "count": 12,
    "details": [
      {"entity": "resource", "field": "topic_suggestion_id", "raw_id": "...", "reason": "node not found"}
    ]
  }
}
```

---

## 8. Open Questions and Design Decisions

### 8.1 Entity Classification

**Q: How should "Report" vs "Request for Comment" be distinguished?**

Currently, both are Resources in Bubble. The `Type1` field provides the primary signal:
- "Publication" → Reports (analytical publications, Capital Markets Bureau reports)
- "Proposed Guidance & Support Materials" → Requests for Comment (exposed proposals)
- "Agenda & Materials" → Meeting materials

Additionally, the `Category` field on linked Agenda Items provides secondary classification (e.g., "SAP Clarification", "New SSAP", "Interpretation"). The new system could create a derived `entity_subtype` column combining these signals.

**Recommendation:** Keep a single `resources` table with a `resource_type` column. Create SQL views for Report, Request for Comment, and Agenda Materials. Do not create separate tables.

### 8.2 Multi-Assignment

**Q: Can one resource belong to multiple agenda items and multiple topics?**

Yes. In Bubble:
- Agenda Item `Resources` is a list → one resource can appear in multiple agenda items' lists
- `topic suggestion` on Resource is single-valued, BUT multiple topics can be inherited from multiple linked agenda items

**Recommendation:** The `agenda_item_resources` join table handles the many-to-many. For topics, store the single `topic_suggestion_id` on the resource AND create a derived `resource_topics` view that unions the direct suggestion with all inherited topics via agenda items.

### 8.3 Historical Inconsistencies

**Q: How to handle the 78 agenda items with empty `Discussed at list`?**

These items are invisible to group-scoped queries. The `BA Ref #` prefix can recover ~41 of them (e.g., "SAPWG#2024-11" implies SAPWG group). For the remaining ~37, they may have the legacy `Discussed at` single field populated, or they may be truly orphaned.

**Recommendation:** During normalization, populate `primary_group_id` using this priority: `Discussed at list[0]` → `Discussed at` → inferred from `BA Ref #` prefix → null. Log all inference in the validation report.

### 8.4 Alert Ownership

**Q: Do alerts belong to calendar items, resources, or both?**

Alerts are created when a new Resource is detected and linked to an existing Calendar Item. The Alert references the Calendar Item (via `Related calendar item`) and the Resource (via `Trigger URL`). So:
- Primary owner: Calendar Item (the alert appears on the meeting)
- Trigger: Resource (what was detected)

**Recommendation:** `alerts` table has `calendar_item_id` (FK) and `trigger_url` (soft join to `resources.url`). The alert belongs to the calendar item.

### 8.5 Deduplication

**Q: Can the same resource appear twice in Bubble?**

Yes — the system uses URL-based deduplication (`find_resources_by_url`), but historical data may have duplicates from before this was implemented.

**Recommendation:** Run a dedup pass during normalization: group by `url`, keep the most recently modified record, log duplicates in the validation report.

### 8.6 Incremental vs Full Extraction

**Q: Should the pipeline do full extractions or incremental updates?**

**Recommendation:** Start with full extraction (fetch everything). The Bubble API doesn't support webhooks or change feeds. Incremental could be added later using `Modified Date` filtering, but the total data volume (~3,000 objects) is small enough that full extraction is practical.

### 8.7 Tree Scope

**Q: Which trees should be normalized?**

Only three trees are relevant to the core entity model:
- **Organization** (140 nodes) — required for NAIC group resolution
- **Chronicles** (87 nodes) — required for topic classification
- **Resources Types** (11 nodes) — required for document type classification

The other 13 trees (Asset Classes, Insurance Entity Type, etc.) are used elsewhere in the Bubble app and can be ignored unless downstream consumers need them.

---

## 9. Example Rows

### 9.1 Calendar Item

**`calendar_items` row:**
```json
{
  "id": "1693984156111x836761350932267000",
  "title": "NAIC SAPWG WG | CLOs and ABS",
  "date": "2023-09-12T14:00:00.000Z",
  "end_time": "2023-09-12T14:30:00.000Z",
  "timezone": "America/New_York",
  "location": null,
  "description": null,
  "is_full_day": false,
  "duration": "0:30",
  "naic_group_id": "1709122405701x990044753610408000",
  "naic_group_name": "Statutory Accounting Principles (E) Working Group",
  "naic_group_path": "NAIC > Financial Condition (E) Committee > Statutory Accounting Principles (E) Working Group",
  "subtopic": "Residuals definition",
  "has_topic": true,
  "created_at": "2023-09-06T07:09:15.857Z",
  "modified_at": "2024-05-30T09:46:58.546Z"
}
```

**Embedding text:**
```
NAIC SAPWG WG | CLOs and ABS
Date: 2023-09-12
Group: NAIC > Financial Condition (E) Committee > Statutory Accounting Principles (E) Working Group
Subtopic: Residuals definition
```

### 9.2 Resource (Report)

**`resources` row:**
```json
{
  "id": "1731019820042x652190297443795000",
  "name": "Climate and Resiliency (EX) Task Force - November 19, 2024 - Meeting Agenda",
  "url": "https://content.naic.org/sites/default/files/national_meeting/CRTF_Agenda_Fall%20NM%2011.19.24.pdf",
  "date": "2024-11-19T08:00:00.000Z",
  "date_display": "Full date",
  "notes": null,
  "domain": "content.naic.org",
  "resource_type": "Agenda & Materials",
  "resource_type_id": "1709756450783x980012943297740800",
  "topic_suggestion_id": "1710771507877x261199898785676740",
  "topic_suggestion_name": "NAIC Climate Initiatives",
  "is_archived": false,
  "is_internal": false,
  "vectorizable": false,
  "parent_resource_id": "1731019820042x652190297443795000",
  "created_at": "2024-11-07T22:50:19.997Z",
  "modified_at": "2024-11-07T22:52:04.459Z"
}
```

**`resource_organizations` rows:**
```json
[
  {
    "resource_id": "1731019820042x652190297443795000",
    "org_node_id": "1709117739780x701904726450241500",
    "org_node_name": "Climate and Resiliency (EX) Task Force"
  }
]
```

**Embedding text:**
```
Climate and Resiliency (EX) Task Force - November 19, 2024 - Meeting Agenda
Type: Agenda & Materials
Date: 2024-11-19
Topic: NAIC Climate Initiatives
Organization: Climate and Resiliency (EX) Task Force
URL: https://content.naic.org/sites/default/files/national_meeting/CRTF_Agenda_Fall%20NM%2011.19.24.pdf
```

### 9.3 Agenda Item

**`agenda_items` row:**
```json
{
  "id": "1715005045580x313445082397541200",
  "ba_title": "ASU 2023-09, Improvements to Income Tax Disclosures",
  "naic_title": "ASU 2023-09, Improvements To Income Tax Disclosures",
  "ba_ref": "SAPWG#2024-11",
  "ref_number": "2024-11",
  "ref_prefix": "SAPWG",
  "description": "Exposure proposes revisions to adopt ASU 2023-09 Improvements to Income Tax Disclosures with modification in SSAP No. 101.",
  "category": "SAP Clarification",
  "status": "Spring 2024 NM - Exposed agenda item.",
  "priority": "A",
  "date_added": "2024-03-16T04:00:00.000Z",
  "proposed_by": "NAIC Staff",
  "ssap_refs": "101",
  "primary_group_id": "1709122405701x990044753610408000",
  "primary_group_name": "Statutory Accounting Principles (E) Working Group",
  "created_at": "2024-05-06T14:17:25.580Z",
  "modified_at": "2024-08-06T23:51:18.832Z"
}
```

**`agenda_item_topics` rows:**
```json
[
  {
    "agenda_item_id": "1715005045580x313445082397541200",
    "topic_id": "1710771343109x631718453703900000",
    "topic_name": "Collateral Loans"
  }
]
```

**`agenda_item_resources` rows:**
```json
[
  {"agenda_item_id": "1715005045580x313445082397541200", "resource_id": "1720470029646x787013784356782100"},
  {"agenda_item_id": "1715005045580x313445082397541200", "resource_id": "1722982383754x268731789695516670"},
  {"agenda_item_id": "1715005045580x313445082397541200", "resource_id": "1722987872719x140517759875547140"},
  {"agenda_item_id": "1715005045580x313445082397541200", "resource_id": "1722988194520x194306162488246270"}
]
```

**Embedding text:**
```
ASU 2023-09, Improvements to Income Tax Disclosures
Reference: SAPWG#2024-11
Category: SAP Clarification
Status: Spring 2024 NM - Exposed agenda item.
Description: Exposure proposes revisions to adopt ASU 2023-09 Improvements to Income Tax Disclosures with modification in SSAP No. 101.
Group: Statutory Accounting Principles (E) Working Group
Topics: Collateral Loans
SSAP References: 101
Proposed By: NAIC Staff
Linked Resources: 4 documents
```

### 9.4 Alert

**`alerts` row:**
```json
{
  "id": "1773174648402x355828714295882700",
  "alert_type": "Agenda Posted",
  "date": "2026-03-10T00:00:00.000Z",
  "calendar_item_id": null,
  "trigger_url": null,
  "created_at": "2026-03-10T20:30:48.402Z"
}
```

### 9.5 Request for Comment (Resource with Type1 = Proposed Guidance)

**`resources` row (filtered view):**
```json
{
  "id": "example_rfc_001",
  "name": "Proposed Revisions to RBC of Tax Credit Investments Held by Property & Casualty",
  "url": "https://content.naic.org/sites/default/files/...",
  "date": "2024-11-18T00:00:00.000Z",
  "resource_type": "Proposed Guidance & Support Materials",
  "topic_suggestion_name": "Tax Credit Structures",
  "notes": "New docs in Capital Adequacy Task Force"
}
```

---

## 10. Recommended Implementation Plan

### Phase 1: Data Extraction (Week 1)

**Goal:** Reliable extraction of all Bubble objects into raw JSON.

1. Set up new Python repo with `pyproject.toml`, basic project structure
2. Implement Bubble API client (can copy/adapt from `web-change-tracker/bubble/client.py`)
3. Write extractors for each type:
   - `extract_resources()` → paginate all Resources
   - `extract_calendar_items()` → paginate all Calendar Items
   - `extract_agenda_items()` → paginate all Agenda Items
   - `extract_tree_nodes(tree_name)` → fetch nodes for Organization, Chronicles, Resources Types
   - `extract_alerts()` → paginate all Alerts
4. Save raw JSON snapshots: `raw/resources.jsonl`, `raw/calendar_items.jsonl`, etc.
5. Add run manifest with timestamps, counts, API call stats

**Deliverable:** `python -m pipeline extract` produces raw JSON snapshot.

### Phase 2: Normalization (Week 2)

**Goal:** Transform raw JSON into normalized tables.

1. Build tree resolution infrastructure:
   - Load all tree nodes into memory
   - Build ID → name, ID → path lookup dicts
   - Handle polymorphic parent references
2. Implement entity normalizers:
   - `normalize_resources(raw) → resources table + resource_organizations + resource_calendar_items`
   - `normalize_calendar_items(raw) → calendar_items table + calendar_item_agenda_items + calendar_item_materials`
   - `normalize_agenda_items(raw) → agenda_items table + agenda_item_topics + agenda_item_resources + agenda_item_groups`
   - `normalize_alerts(raw) → alerts table`
   - `normalize_trees(raw) → chronicle_topics + org_nodes + resource_types`
3. Write output as parquet + jsonl

**Deliverable:** `python -m pipeline normalize` produces all tables in `output/tables/`.

### Phase 3: Join Resolution and Validation (Week 3)

**Goal:** Verify referential integrity and produce quality reports.

1. Validate all foreign keys resolve:
   - Every `resource_type_id` points to a valid `resource_types.id`
   - Every `naic_group_id` points to a valid `org_nodes.id`
   - Every join table FK points to a valid entity
2. Compute coverage metrics:
   - % of resources with topic suggestion
   - % of agenda items with topics
   - % of agenda items with group assignment
   - % of resources with calendar links
3. Detect and report:
   - Orphaned IDs (references to nonexistent entities)
   - Duplicate resources (same URL)
   - Agenda items with no group (neither Discussed at list nor inferable prefix)
4. Write `output/validation/validation_report.json`

**Deliverable:** `python -m pipeline validate` produces validation report with coverage stats.

### Phase 4: Embedding Text Generation (Week 3-4)

**Goal:** Produce embedding-ready text views.

1. Implement embedding text builders per entity type (see Section 5.1)
2. For each entity, join related entities to build rich text:
   - Resources: join agenda items, topics, calendar items, organizations
   - Calendar Items: join agenda items, topics, materials, alerts
   - Agenda Items: join topics, resources, groups
   - Chronicle Topics: join agenda items, resource counts
3. Write `output/embeddings/*.jsonl` with `{id, entity_type, text, metadata}`

**Deliverable:** `python -m pipeline embed` produces embedding-ready JSONL files.

### Phase 5: Vectorization Layer (Week 4-5)

**Goal:** Generate and store embeddings.

1. Choose embedding model (recommended: `text-embedding-3-small` or `text-embedding-3-large`)
2. Batch-embed all entity texts
3. Store vectors in a vector database or as parquet with numpy arrays
4. Implement basic retrieval: `query(text, entity_type, filters) → top_k results`
5. Add metadata filtering: date ranges, resource types, NAIC groups

**Deliverable:** `python -m pipeline vectorize` produces embeddings; `python -m pipeline query "search text"` demonstrates retrieval.

### Phase 6: Scheduling and Incremental Updates (Week 5+)

**Goal:** Production-ready pipeline.

1. Add `--incremental` mode using `Modified Date` filtering
2. Add S3 output support for versioned snapshots
3. Add scheduling (cron or EventBridge)
4. Add diff reporting: what changed since last run
5. Integration tests against live Bubble API

---

## Appendix A: Bubble API Reference

### Authentication

```
GET https://{app-name}.bubbleapps.io/version-{version}/api/1.1/obj/{type}
Authorization: Bearer {BUBBLE_API_KEY}
```

### Constraint Types

| Constraint | Description | Example |
|-----------|-------------|---------|
| `equals` | Exact match | `{"key": "Name", "constraint_type": "equals", "value": "foo"}` |
| `text contains` | Substring match | `{"key": "BA title", "constraint_type": "text contains", "value": "tax"}` |
| `greater than` | Date/number comparison | `{"key": "date", "constraint_type": "greater than", "value": "2024-01-01"}` |
| `less than` | Date/number comparison | Similar |
| `in` | List membership | `{"key": "Discussed at list", "constraint_type": "in", "value": "node_id"}` |
| `contains` | List field contains value | `{"key": "Resources", "constraint_type": "contains", "value": "resource_id"}` |

### Pagination

```
GET /obj/{type}?limit=100&cursor=0
Response: { "results": [...], "count": 100, "remaining": 50, "cursor": 100 }
```

Use `cursor` for pagination. `remaining > 0` means more pages.

### Environment Variables

```
BUBBLE_API_URL=https://{app}.bubbleapps.io/version-live/api/1.1
BUBBLE_API_KEY=<bearer token>
```

---

## Appendix B: BA Ref # Prefix to Group Mapping

This mapping is critical for inferring group membership when `Discussed at list` is empty.

| Prefix | NAIC Group |
|--------|-----------|
| SAPWG | Statutory Accounting Principles (E) Working Group |
| VOSTF | Valuation of Securities (E) Task Force |
| LATF | Life Actuarial (A) Task Force |
| BWG | Blanks (E) Working Group |
| LRBCWG | Life Risk Based Capital (E) Working Group |
| RBC-IRE / RBC-IRE-WG | Risk Based Capital Investment Risk and Evaluation (E) Working Group |
| CATF / CA | Capital Adequacy (E) Task Force |
| RAWG | Receivership and Insolvency (E) Task Force |
| SSWG | Structured Securities Group |
| MWG | Macroprudential (E) Working Group |
| FTF | Financial Stability (E) Task Force |
| IAIS | The International Association of Insurance Supervisors |
| BMA | Bermuda Monetary Authority |
| EIOPA | European Insurance and Occupational Pensions Authority |
| PRA | Prudential Regulation Authority (UK) |
| FIO | Federal Insurance Office |
| FACI | Federal Advisory Committee on Insurance |
| E-Committee | Executive (EX) Committee |

Regex for extraction: `^(RBC-IRE-WG|RBC-IRE|LRBCWG|E-Committee|SAPWG|VOSTF|LATF|BWG|CATF|RAWG|SSWG|MWG|FTF|IAIS|BMA|EIOPA|PRA|FIO|FACI|CA)(?:[#\s\-_]|$)`

---

*Generated from the web-change-tracker codebase on 2026-03-20. This document should be used as the foundation for implementation in a new repository.*
