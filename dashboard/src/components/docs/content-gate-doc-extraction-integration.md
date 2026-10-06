# Content Gate — Document Extraction Integration

This document describes how the Content Gate modal fetches, enriches, and displays
data from the document extraction agent pipeline, and how each field maps to Bubble.

---

## 1. What the document extraction pipeline does

For every alert row that contains a library item (a PDF or document), the pipeline runs
a separate **document extraction agent** (`bubble/document_agent.py`) against the PDF.
That agent reads the document via pgvector knowledge-base search and produces one
structured output row **per agenda item found in the PDF**.

These rows are stored in `alerts/document_extractions_table.jsonl` in S3. The
dashboard reads them via `/api/document-extractions`.

---

## 2. Join key: how alert rows link to doc extraction rows

| Field | Scope | Notes |
|-------|-------|-------|
| `agent_call_id` | shared between alert row and all its doc extraction rows | UUID per `extract_page_change()` invocation; every alert produced by that call, and every doc extraction produced from those alerts, carries this UUID |
| `library_item_url` | discriminates which doc extraction rows belong to which alert row | one page change can produce multiple alert rows (multiple PDFs); each PDF gets its own doc extraction run and its rows all share the same `library_item_url` |
| `doc_extraction_id` | identifies a single `extract_document_data()` call | all rows for the same PDF share this UUID; differs from `agent_call_id` |

**Lookup pattern used by the content gate:**
```
GET /api/document-extractions?agent_call_id=<id>
  → returns all doc extraction rows for this agent call
  → filter client-side to rows where row.library_item_url === alert.library_item_url
  → remaining rows are this alert's doc extraction rows, one per agenda item
```

The `agent_call_id` filter is sufficient for most cases (single-library-item page changes).
The `library_item_url` secondary filter is required when one page change produced multiple
library items (multiple PDFs detected on the same page).

---

## 3. Doc extraction agent output fields

Everything in `storage/doc_schema.py::DOC_PIPELINE_FIELDS` is pipeline-stamped and NOT
produced by the agent. All other fields on a doc extraction row are agent output.

### 3a. Document-level fields (same on every row for the same PDF)

| Field | Type | Description | Maps to Bubble |
|-------|------|-------------|----------------|
| `document_type` | string | Agent's classification of the document. Examples: `"Agenda"`, `"Minutes"`, `"Exposure Draft"`, `"Agenda & Materials"` | `libraryitem` › `Type - DT` (resolved via `/obj/libraryitemtype` at sync time) |
| `document_title` | string | Agent's extracted title for the document | Used as fallback for `libraryitem` › `name_text` if `library_item_preliminary_title` is absent |
| `updated_or_new_document` | string | Whether this document is new or an update to an existing one. Values: `"New"`, `"Updated"`, `"Existing"` | Drives `bubble_action.library_item` = `"create"` vs `"update"` in the classifier |
| `date_published` | string | Publication or effective date. ISO 8601 or natural language (`"June 22, 2026"`) | `libraryitem` › `date` (display: "Full date"); `parseDate()` normalizes to `YYYY-MM-DD` |
| `meeting_or_last_comment_date` | string | Date the associated meeting occurred or RFC comment period closed | Informational; not directly written to Bubble |
| `document_description` | string | Agent-generated summary of the document's purpose or content | `libraryitem` › `description_text` (capped to 2000 chars at sync time) |
| `organization_or_publisher` | string | Organization that published the document | Informational display; NOT written to Bubble directly — `organization` on the parent alert row is used for `libraryitem` › `organizations` |
| `is_the_document_relevant_for_a_future_newsreel_article` | string | Agent's yes/no/review assessment of newsreel relevance. Alias: `newsreel_relevance` | Controls `ingest_status: "pending"` on the doc extraction row (set by `spike.py`) — triggers the Newsreel › Document section in the content gate |

### 3b. Per-agenda-item fields (one row per agenda item in the PDF)

| Field | Type | Description | Maps to Bubble |
|-------|------|-------------|----------------|
| `agenda_item_title` | string | BA (Bridgeway Analytics) standardized title for this agenda item | `agendaitem` › `title` (field name in Bubble: `"BA title"`) |
| `agenda_item_title_official` | string | Official NAIC title as printed in the document | `agendaitem` › `NAIC Title` |
| `agenda_item_official_id` | string | Official NAIC identifier (e.g., `"E/2025-01"`) | `agendaitem` › `NAIC Item #` |
| `agenda_item_standardized_id` | string | BA standardized reference ID (e.g., `"E-2025-001"`) | `agendaitem` › `BA Ref #` |
| `chronicle_topics` | string[] | Chronicle topic names for this agenda item. Topics are drawn from the fixed Bubble taxonomy. | `agendaitem` › `topics - dt` (IDs resolved via `/obj/chronicletopic` at sync time) |
| `number` | string | Agenda item number as listed in the document | Informational; shown in doc extractions table; not written directly to Bubble |
| `existing_or_new_agenda_item` | string | Whether this is a new or existing agenda item in Bubble. Values: `"New"`, `"Existing"`, `"Updated"` | Drives `status` field on agenda item preview → determines CREATE vs look-up-and-link at sync time |

---

## 4. Pipeline enrichment: what `enrich_with_doc_extraction()` already does

`bubble/bubble_sync_classifier.py::enrich_with_doc_extraction(bubble_action, extraction)`
is called at pipeline time (in `spike.py`) for every alert row that has doc extraction
results. It mutates `bubble_action` in place and then this enriched dict is stored in
`alerts_table.jsonl`.

**What it enriches:**

| `bubble_action` field | Source field in `extraction` | Notes |
|-----------------------|------------------------------|-------|
| `event_preview.field_ids["Topics - dt"]` | Agenda items' `chronicle_topics` union | Only set if not already present |
| `agenda_item_previews[]` | Parsed from `extraction["agenda_items"]` string (old format) | Replaces with doc-extraction-derived items; filters procedural N/A items |
| `library_item_preview.field_ids["description_text"]` | `extraction["document_description"]` | Only set if not already present |
| `library_item_preview.field_ids["date_date"]` | `extraction["date_published"]` | Only set if not already present |
| `library_item_preview.fields["Doc Type"]` | `extraction["document_type"]` | Display only; ID resolved at sync time |
| `library_item_preview.field_ids["Topics - dt"]` | Topic union from agenda items | Only set if not already present |

**Important note on schema versions:**

The `agenda_items` string field used by `enrich_with_doc_extraction()` is the **old**
doc extraction schema format. New doc extraction runs produce **one row per agenda item**
with flat fields (`agenda_item_title`, `chronicle_topics`, etc.) instead of a formatted
string. For rows processed with the new schema, the content gate must read the flat rows
directly rather than relying solely on the parsed string format.

---

## 5. Content gate integration architecture

### 5a. Data flow

```
AlertsTable
  └─ "Actions" button on row with bubble_action
       └─ ContentGateModal opens
            ├─ Reads bubble_action from alert row
            │   └─ agenda_item_previews (enriched at pipeline time)
            │   └─ library_item_preview.field_ids (enriched at pipeline time)
            │   └─ event_preview.field_ids
            ├─ Fetches /api/bubble/validate?agent_call_id=xxx (pre-flight)
            ├─ Fetches /api/document-extractions?agent_call_id=xxx (doc extraction rows)
            │   └─ Filters to rows where library_item_url matches
            │   └─ Used to enrich agenda item previews with fresher data
            └─ Fetches /api/bubble/record (when action=update, to show current Bubble values)
```

### 5b. Why fetch doc extraction rows in the content gate?

The `bubble_action` stored in `alerts_table.jsonl` was computed at pipeline time. If the
user has since re-run the doc extraction agent on the same row (via the Doc Extractions
page), the `bubble_action` may have stale chronicle topics, official titles, or reference
IDs. Fetching the live doc extraction rows at modal-open time gives us the most current
extraction output to display.

Additionally, `bubble_action.agenda_item_previews` was populated from the old string-format
`agenda_items` field. Live doc extraction rows (new multi-row schema) carry per-item fields
directly on each row, giving more granular and reliable data.

### 5c. Enrichment strategy in the content gate

The content gate applies doc extraction data as a **display overlay** on top of
`bubble_action.agenda_item_previews`. Enrichment is applied for rendering only — the
data-write path (what gets sent to Bubble when the user clicks "Create Agenda Items")
still uses the `bubble_action` data that has already been potentially patched by the user
via the title-edit inputs and deletion controls.

**Enrichment fields per agenda item:**

| `AgendaItemPreview` field | Source priority |
|--------------------------|-----------------|
| `title` | User edits → `bubble_action` (not overridden by doc extraction) |
| `status` | `bubble_action` (not overridden) |
| `chronicle_topics` | Doc extraction row `chronicle_topics[]` (if non-empty) → `bubble_action` fallback |
| `official_title` | Doc extraction row `agenda_item_title_official` (if non-NA) → `bubble_action` fallback |
| `reference_id` | Doc extraction row `agenda_item_standardized_id` (if non-NA) → `bubble_action` fallback |

**Matching logic:** agenda items are matched by title. The doc extraction
`agenda_item_title` is compared (case-insensitive, after stripping status prefixes like
"New - " / "Existing - ") against the `bubble_action` item's `title` (also stripped).

---

## 6. Content gate sections that use doc extraction data

### 6a. "Create Agenda Items" card (standalone, before Library Item)

Shows each new agenda item with:
- Editable BA title input (from `bubble_action`, user-editable)
- Chronicle topic chips — **enriched from doc extraction** (fresher topics shown)
- × delete button (removes item from creation list)

### 6b. "Create/Update Library Item" card

Field table includes:
- **Name**: from `library_item_preview.title` (alert row `library_item_preliminary_title`)
- **URL**: from `library_item_preliminary_url`
- **Type**: from `library_item_preview.type` → resolved to Bubble type ID at sync time
- **Date**: from `library_item_preview.field_ids["date_date"]` (set by pipeline enrichment from `date_published`)
- **Summary**: from `library_item_preview.field_ids["description_text"]` (set by pipeline enrichment from `document_description`)
- **Chronicle Topics**: union of all agenda item topics + doc-extraction-level topics
- **Linked Agenda Items**: list of all agenda items (new + existing) — uses enriched items
- **Newsreel Relevance**: read-only badge from doc extraction `is_the_document_relevant_for_a_future_newsreel_article` — indicates whether doc extraction agent flagged this document for the newsreel knowledge base

### 6c. "Create/Update Event" card

- **Chronicle Topics**: `allLibraryItemTopics(event_preview, enrichedAgendaItems)` — union of event-level topics + all enriched agenda item topics
- **Linked Agenda Items**: enriched agenda item titles
- **Agenda**: linked library item title

---

## 7. Bubble field reference (sync payload)

### Library Item (`wf/create-library-item/`)

| Bubble field | Payload key | Source |
|-------------|-------------|--------|
| Name | `title` | `library_item_preview.field_ids["name_text"]` or `lp.title` |
| URL | `url` | `library_item_preview.field_ids["url_text"]` or `lp.url` |
| Type | `type` | Type ID resolved from `lp.type` via `/obj/libraryitemtype` |
| Organizations | `organizations` | Org IDs resolved from org names at sync time |
| Date | `date` | `parseDate(event_start_date_time)` → fallback to `field_ids["date_date"]` |
| Date Display | `date_display` | Always `"Full date"` |
| Summary | `summary` | `field_ids["description_text"]` (from `document_description`) |
| Chronicle Topics - dt | `chronicle_topics` | IDs resolved from agenda item topics union |
| Agenda Items | `agenda_items` | Bubble IDs of created/linked agenda items |

### Agenda Item (`wf/create-agenda-item/`)

| Bubble field | Payload key | Source |
|-------------|-------------|--------|
| BA title | `title` | `agenda_item_previews[i].title` (after stripping status prefix) |
| NAIC Title | `official_title` | `agenda_item_previews[i].official_title` (from doc extraction) |
| BA Ref # | `reference_id` | `agenda_item_previews[i].reference_id` (from doc extraction `agenda_item_standardized_id`) |
| Topics - dt | `chronicle_topics` | IDs resolved from `agenda_item_previews[i].chronicle_topics` |
| Eiderix Ref | `alert_id` | `agent_call_id` |

### Calendar Item (Data API `POST /obj/calendaritem`)

| Bubble field | Payload key | Source |
|-------------|-------------|--------|
| title | `title` | `event_preview.title` |
| Topics - dt | `Topics - dt` | IDs — union of event topics + all agenda item topics |
| Orgs | `Orgs ` (trailing space) | Org IDs |
| Agenda | `Agenda` | `[bubble_library_item_id]` |
| Attached agenda items | `attached agenda items` | Bubble agenda item IDs |
| date | `date` | `event_start_date_time` as UTC ISO |
| End time | `End time` | `event_end_date_time` as UTC ISO |
| AMPM from/until | computed | Derived from UTC start/end (see `computeEventTimeFields`) |
| Hour/Minutes from/until | computed | Same |
| length | computed | Duration in `H:MM` format |
| full day | `full day` | `event_is_full_day === "Full Day"` |
| datewasset | `datewasset` | `true` when start date is present |
| has topic | `has topic` | `eventTopicIds.length > 0` |
| name for search | `name for search` | `event_title.toLowerCase()` |
| event description | `event description` | `alert_description` |

---

## 8. Pre-flight validation (`/api/bubble/validate`)

Before the user confirms any Bubble publish, the content gate runs a parallel validation:
- Org name → Bubble ID resolution (using 3-tier match: exact → strip NAIC suffix → prefix)
- Chronicle topic name → Bubble ID resolution (exact match)
- Library item type name → Bubble ID resolution
- Calendar item type name → Bubble ID resolution

Failures are shown in the **ValidationBanner** at the top of the modal sections:
- `severity: "error"` — red: org not found (record will have empty Organizations field)
- `severity: "warning"` — amber: topic not found (empty-topic fallback will be used)

These are display-only warnings — the user can still click through and publish.

---

## 9. State management in ContentGateModal

```
bubble_action           (from alert row, read-only reference)
  ├─ agenda_item_previews        → newAgendaItems / existingAgendaItems
  ├─ event_preview               → eventPreview
  └─ library_item_preview        → libPreview

User edits (React state):
  ├─ deletedNewIndices           Set<number> — indices of removed new agenda items
  ├─ agendaItemTitleEdits        Record<number, string> — edited titles
  ├─ eventEdits                  Record<string, string> — edited event fields
  └─ libEdits                    Record<string, string> — edited lib item fields

Doc extraction data (fetched async on open):
  └─ docExtractionRows           Array<Record<string, unknown>> — filtered by library_item_url

Derived for rendering:
  ├─ activeNewAgendaItems        newAgendaItems + title edits + deletions (used for patch/sync)
  ├─ activeAgendaItems           activeNewAgendaItems + existingAgendaItems (used for patch/sync)
  └─ displayedAgendaItems        activeAgendaItems enriched with doc extraction data (display only)

Validation state:
  └─ validation                  null | "loading" | ValidationResult

Section states:
  └─ sections                    Record<string, SectionState> — "idle"|"loading"|"done"|"error"
```

---

## 10. Key invariants

- **Enrichment is display-only.** The actual Bubble sync payload is built from
  `bubble_action.agenda_item_previews` (possibly patched by user edits), not from live
  doc extraction rows. If the user wants freshly-extracted topics in Bubble, they should
  accept a doc rerun first, then re-open the content gate.

- **`library_item_url` is the definitive join key.** Use it to filter doc extraction rows
  to the specific library item shown in the content gate. Without this filter, multi-PDF
  page changes would blend topics from unrelated documents.

- **Chronicle topics flow upward.** Agenda item topics → library item topics →
  calendar item topics (via `allLibraryItemTopics`). The union ensures the event in Bubble
  has all topics from all its agenda items.

- **`organization_or_publisher` ≠ `organization`.** The doc extraction's
  `organization_or_publisher` is the document publisher as the agent understood it from
  the PDF. The alert row's `organization[]` is the NAIC org the pipeline classified the
  alert under. The latter is what gets sent to Bubble — the former is informational.
