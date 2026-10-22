# Content Gate — Technical Reference

The **Content Gate** is the per-row publishing modal in the Alerts page. From a single
UI it drives three Bubble (Eidarix) write workflows plus ingest into the Newsreel
Knowledge Base, with pre-flight validation, searchable dropdowns for every
Bubble-referenced field, and field-level mapping between the agent's output schema
and the Bubble data model.

Primary file: `src/components/ContentGateModal.tsx`
Executor:     `src/lib/bubble-sync-eidarix.ts`
Routes:       `src/app/api/bubble/{sync,validate,record,options}/route.ts`
Dropdown:     `src/components/SearchableCombobox.tsx`

---

## 1. What publishes where

For a given alert row, up to three records are written to Bubble and the row is
optionally ingested into the Newsreel Knowledge Base.

| Target                  | Backing endpoint                               | When it fires |
|-------------------------|------------------------------------------------|---------------|
| Agenda items (0..N)     | `POST version-live/wf/create-agenda-item/`     | `bubble_action.agenda_items === true` and at least one preview with `status: "New"` |
| Library item (0..1)     | `POST version-live/wf/create-library-item/`    | `bubble_action.library_item === "create"` or `"update"` |
| Calendar event (0..1)   | `POST version-live/wf/create-event` or `wf/update-event` | `bubble_action.event === "create"` or `"update"` |
| Newsreel KB (0..1)      | `POST chat-api.bridgewayanalytics.com/internal/documents/ingest` | Row has a `transcript_s3_key` (transcript) or `library_item_url` (document) |

Writes execute **serially** in the order above because later records link to earlier
ones (library item links to its agenda items; event links to its library item and
agenda items). The modal enforces this dependency: a step's button stays disabled
until its predecessor has succeeded.

---

## 2. Bubble space model

Bubble has two separate concepts that are easy to confuse:

1. **Version** (`version-test`, `version-live`) — infrastructure environment. Each
   version has its own database.
2. **Space** (a tag on every record) — tenant/content partition within a version.

| Role                         | ID                                       | Used for |
|------------------------------|------------------------------------------|----------|
| Content space (`CONTENT_SPACE_ID`) | `1768998437948x865417918648382000` | Where organizations, chronicle topics, library item types, and calendar item types live. Reference data lookups scope by this. |
| Output space (`SPACE_ID`)          | Equals content space in practice.   | What goes in the `space_id` field of workflow payloads. Must match the content space so the workflow can resolve the linked chronicle topics / orgs. |

The sync executor keeps these as two named constants (`CONTENT_SPACE_ID` vs
`SPACE_ID`). Both resolve to the same ID today; the distinction exists so that
future multi-space deployments can be added without touching every call site.

`EIDARIX_VERSION=live` → write workflows hit `version-live/api/1.1/wf/...` (live
Bubble database). Reference lookups use the non-versioned Data API (`/api/1.1/obj/...`),
which maps to the deployed version.

---

## 3. Data flow when the modal opens

```
AlertsTable row with bubble_action → user clicks "Actions"
  └─ ContentGateModal mounts
       ├─ Reads bubble_action from the alert row (static reference)
       │     • agenda_item_previews[]
       │     • library_item_preview
       │     • event_preview
       │
       ├─ GET  /api/schema                          → agent-field → human label map
       ├─ GET  /api/bubble/options?type=X (×4)      → org / topic / libtype / caltype
       │                                              dropdown options (10-min cache)
       ├─ GET  /api/bubble/validate?agent_call_id=X → pre-flight check
       ├─ GET  /api/document-extractions?agent_call_id=X
       │                                            → doc extraction rows, filtered
       │                                              by library_item_url
       └─ For UPDATE sections only:
           GET /api/bubble/record?agent_call_id=X&object_type=calendaritem|libraryitem
                                                    → current Bubble values
```

All five requests fire in parallel on open. The modal renders progressively; dropdowns
show a "Loading…" placeholder until options arrive.

---

## 4. Where `bubble_action` comes from

`bubble_action` is stamped on each alert row at pipeline time by
`bubble/bubble_sync_classifier.py::classify_alert()`, then enriched by
`enrich_with_doc_extraction()` for rows with doc extraction output.

```json
{
  "bubble_action": {
    "event": "create" | "update" | null,
    "library_item": "create" | "update" | null,
    "agenda_items": true | false,
    "event_preview": {
      "title", "start_datetime", "end_datetime", "group", "url", "call_in",
      "match_key", "what_changes",
      "fields":    { "<Bubble display name>": "<value>", ... },
      "field_ids": { "<Bubble API field name>": "<value>", ... },
      "match_search": { "org": "...", "date": "YYYY-MM-DD" }
    },
    "library_item_preview": {
      "title", "url", "filename", "type", "group",
      "what_changes",
      "fields":    { "Name": "...", ... },
      "field_ids": { "name_text": "...", ... },
      "match_search": { "url": "...", "title": "..." }
    },
    "agenda_item_previews": [
      { "title", "status", "chronicle_topics", "official_title", "reference_id" }
    ],
    "notes": "<alert_type>"
  }
}
```

Only set when the classifier judged the alert publishable; absent on
"No Meaningful Change" and carousel alerts.

### Classifier library-item type map

`_LIBRARY_ITEM_TYPE` in `bubble_sync_classifier.py` uses the exact Bubble taxonomy
names so the sync executor can resolve type IDs without name translation:

| Alert type                                     | Bubble `libraryitemtype.Title`              |
|------------------------------------------------|---------------------------------------------|
| New/Updated Agenda, Materials, Agenda & Materials | "Agenda & Materials"                     |
| New/Updated Request for Comment                | "Proposed Guidance & Support Materials"     |
| New/Updated Effective Date                     | "Existing Requirements & Guidance"          |
| New or Updated Report or Other Resource        | "Publication"                               |
| New Meeting Transcript Available               | "Other"                                     |
| Other                                          | "Publication"                               |

---

## 5. The 3-column field table

Every field inside a section renders in a 3-column table:

| Column        | Content                                                           |
|---------------|-------------------------------------------------------------------|
| Agent Field   | Human-readable label from the agent's output schema (from `/api/schema`). Falls back to a hand-maintained map in `AGENT_FIELD_LABEL_FALLBACKS` for internal/derived fields. |
| Bubble Field  | Name of the Bubble field being written (e.g., "Name", "Date"). |
| Value         | Either a text input (free-text), a `SearchableCombobox` (dropdown), chronicle-topic chips, a numbered list, or read-only text. |

For UPDATE sections, when the modal has fetched the current Bubble record,
unchanged fields display the authoritative Bubble value (`mergeWithCurrentRecord`),
while changed fields show the proposed new value highlighted amber.

### SearchableCombobox

`src/components/SearchableCombobox.tsx` is a single reusable combobox used
anywhere the field being edited is a reference lookup. Supports `single` and
`multi` modes. Opens with a search input focused; `Esc` closes.

Multi-select values are stored in the per-section `editState` map
(`Record<string, string>`) as `MULTI_SEP`-joined strings; `multiToEditValue` and
`multiFromEditValue` convert both ways. The `applyEditsToPreview` function knows
how to split them back into string arrays for the sync payload.

### Which fields are backed by comboboxes

| Field                          | Mode   | Options source                | API cached |
|--------------------------------|--------|-------------------------------|-----------|
| Library Item › Type            | single | `/obj/libraryitemtype` (versioned URL, no space constraint) | 10 min |
| Library Item › Organizations   | multi  | `/obj/organization` scoped to `CONTENT_SPACE_ID` | 10 min |
| Library Item › Chronicle Topics | multi | `/obj/chronicletopic` scoped to `CONTENT_SPACE_ID` | 10 min |
| Event › Type                   | single | `/obj/calendaritemtype` (versioned URL, no space constraint) | 10 min |
| Event › Organizations          | multi  | `/obj/organization` scoped to `CONTENT_SPACE_ID` | 10 min |
| Event › Chronicle Topics       | multi  | `/obj/chronicletopic` scoped to `CONTENT_SPACE_ID` | 10 min |
| Agenda Item › Chronicle Topics (per item) | multi | same as above | 10 min |

Reference-data fetches live in `/api/bubble/options?type=X` with an in-process
10-minute cache (`src/app/api/bubble/options/route.ts`).

### Fields shown vs. what the sync actually writes

The field rows displayed on each card are limited to the set of real Bubble fields
the executor can write. Agent-derived display-only fields that didn't correspond
to a real Bubble write (previously `Doc Type` on library items, `File Name` on
library items, `Call-In` on events) have been removed — the modal is now a faithful
preview of what will land in Bubble.

Current field roster (verified against live library/calendar item records):

- **Library item**: Name → `Name`, URL → `URL`, Type → `Type - DT`, Organizations
  → `Organizations`, Date → `date` + `Date display`, Description → `summary`,
  Chronicle Topics → `Topics - dt`, Linked Agenda Items → `Agenda items`.
- **Calendar event**: Title → `title`, Type → `type`, Start/End Date/Time →
  `date` + `End time` + `AMPM/Hour/Minutes from/until`, Full Day → `full day`,
  Organizations → `Orgs ` (note the trailing space), Location → `location`,
  Chronicle Topics → `Topics - dt`, Linked Agenda Items → `attached agenda items`,
  Agenda → `Agenda` (array of library item IDs).

---

## 6. Section layout

Each section (library item, event, agenda items, standalone vector store) is
rendered by the `Section` component with a consistent header:

```
┌──────────────────────────────────────────────────────────┐
│ SYSTEM · OBJECT TYPE   [ACTION BADGE]           ✓ Done   │  ← header bar
│ <content title shown prominently>                        │  ← subtitle
├──────────────────────────────────────────────────────────┤
│ [3-column field table]                                   │
├──────────────────────────────────────────────────────────┤
│                 [Add to Vector Store ▼] [Create in Bubble] │ ← footer
└──────────────────────────────────────────────────────────┘
```

- **SYSTEM** = `Bubble` or `Newsreel` (color-coded).
- **OBJECT TYPE** = `Library Item`, `Calendar Event`, `Meeting Transcript`,
  `Library Document`, etc.
- **ACTION BADGE** = `CREATE` (green), `UPDATE` (blue), or `PUBLISH` (purple).
- **Subtitle** = the content's title (e.g., library item name, event name),
  shown prominently so users know what's being published.

---

## 7. Date derivation (library item)

The `Date` on a library item is **derived** from its Bubble type regardless of what
the agent originally produced, per the project convention:

| Bubble library-item type                       | Date source                       |
|------------------------------------------------|-----------------------------------|
| Agenda & Materials                             | `event_start_date_time` (meeting date) |
| Proposed Guidance & Support Materials (RFC)    | `event_start_date_time` (last comment date) |
| Existing Requirements & Guidance (Effective)   | `event_start_date_time` (effective date) |
| Publication                                    | doc extraction `date_published` |
| Meeting Transcript / Other                     | `event_start_date_time` (meeting date) |

Computation lives in `deriveLibraryItemDate(libPreview, row, docExtractionRow)` in
`ContentGateModal.tsx`. The derived value is shown as the default; the user can
override by typing. On publish, if the user never typed in the Date field, the
derived value is injected into `libEdits["Date"]` before `applyEditsToPreview`
runs, guaranteeing the correct date reaches Bubble.

The sync executor accepts both ISO (`YYYY-MM-DD`) and natural-language
(`Month DD, YYYY`) formats via `parseDate()` and normalizes to ISO.

---

## 8. Vector store (Newsreel) consolidation

Documents and transcripts flow to the Newsreel Knowledge Base via the same
`/internal/documents/ingest` API. When a library item is already being written to
Bubble, the vector-store action is folded into the Library Item card's footer as a
secondary button:

```
[Add to Library Vector Store ▼]   [Create in Bubble]
    ├─ Newsreel Knowledge Base
    └─ (future types...)
```

- `AddToVectorStoreButton` component renders the split-button pattern.
- Clicking the dropdown item fires `handlePublishTranscript()` or
  `handlePublishDocument()` depending on the row kind:
  - Row has `transcript_s3_key` and `alert_type === "New Meeting Transcript Available"` → `POST /api/ingest/approve-transcript`
  - Otherwise, if `library_item_url` is non-empty and starts with `http` → `POST /api/ingest/approve-document`
- Per-type section state (`sections.transcript` / `sections.document`) renders
  `✓ Added` / `Working…` / `Failed` inside the dropdown.

If there's no library item action to piggy-back on (rare), a standalone section
with just the vector-store dropdown is rendered.

---

## 9. Agenda item editing

Each new agenda item renders as a compact row inside the "Create Agenda Items"
card:

```
1. [editable title input          ]  [×]
   [Chronicle Topics searchable multi-select combobox]
```

- **Title** — free-text input (`agenda_item_title_chronicle_topics[].agenda_item_title`)
- **Chronicle Topics** — `SearchableCombobox` with multi-select, options from the
  10-min-cached chronicle topic list (scoped to `CONTENT_SPACE_ID`)
- Per-item edits are stored in `agendaItemEdits: Record<number, Record<string, string>>`
  and applied via `applyAgendaEdits(item, i)` before patching `bubble_action.agenda_item_previews`

**Status prefixes** (`"New - "`, `"Existing - "`, `"Updated - "`) are stripped at
display time via `stripStatusPrefix()` and also before any Bubble API write.

**Existing/Updated items** (status != `"New"`) are not re-created — they are
looked up in Bubble by title via `findAgendaItemByTitle()` (searches for the "title"
or "BA title" field). If the item can't be found, the whole sync fails with a
`RuntimeError` rather than silently proceeding.

---

## 10. Pre-flight validation

`validateSyncPlan(agentCallId, rows)` in `bubble-sync-eidarix.ts` is called at
modal open via `GET /api/bubble/validate`. It runs four parallel lookups:

| Check                      | Severity | Result                                                   |
|----------------------------|----------|----------------------------------------------------------|
| Unresolved org names       | error    | Record will publish with empty Organizations field       |
| Unresolved chronicle topics | warning  | "Calendar Events with no Topic" fallback will be used    |
| Library item type not found | error   | Library item will publish with no type                   |
| Calendar item type not found | error  | Event will publish with no type                          |

Issues render in the `ValidationBanner` at the top of the modal. Users can still
confirm and publish — the banner is informational, not blocking.

A second pre-ECS check runs when the user clicks "Create in Bubble": if orgs are
still unresolved at that moment (e.g., user removed an org via the combobox and
re-added one that doesn't exist in Bubble), a modal popup asks for confirmation.
Clicking "Create Anyway" calls the same route with `skip_validation: true`.

---

## 11. Sync executor (write path)

`src/lib/bubble-sync-eidarix.ts::syncAlert(agentCallId, action, rows)` is called
from `POST /api/bubble/sync`. The route runs the executor **in-process** inside
the dashboard ECS task — no Lambda intermediary.

### Processing order for `action: "all"`

```
1. Resolve ref IDs:   org names → IDs    (via CONTENT_SPACE_CONSTRAINT)
                      topic names → IDs  (via CONTENT_SPACE_CONSTRAINT)
2. Agenda items:      For each preview with status "New"  → POST wf/create-agenda-item/
                      For each with "Existing"/"Updated" → findAgendaItemByTitle()
                      Collect bubble_ids into eidarix_agenda_item_ids
                      PATCH alert row with eidarix_agenda_item_ids
3. Library item:      Resolve libraryitemtype ID
                      CREATE  → POST wf/create-library-item/ (full payload)
                      UPDATE  → find by url/title, POST wf/update-library-item/
                      PATCH alert row with bubble_library_item_id
4. Calendar event:    Resolve calendaritemtype ID
                      CREATE  → POST wf/create-event (full payload)
                      UPDATE  → find by match_search, POST wf/update-event (trimmed:
                                 only id, alert_id, space_id, chronicle_topics,
                                 agenda_items, agenda)
                      PATCH alert row with bubble_event_id
```

Clicking individual section buttons calls `action: "agenda_items"`, `"library_item"`,
or `"event"` — each runs just that step and stamps the resulting ID on the row.

### Payloads

All writes send:
- `space_id: CONTENT_SPACE_ID` — required for reference lookups to resolve
- Reference fields (`chronicle_topics`, `organizations`, `type`) as Bubble `_id` strings
- `alert_id: agent_call_id` so Bubble records are linkable back to the dashboard row

### Error handling

- Missing orgs → warn and continue with empty org list (does not raise)
- Missing chronicle topics → warn, fall back to the "empty topic" chronicle record
- Library/event UPDATE lookup not found → raise `RuntimeError`
- Agenda item lookup failure (Existing/Updated) → raise `RuntimeError`
- Any workflow 4xx/5xx response → raise with status + response body

### Full-day events

`computeEventTimeFields(startUtc, endUtc, isFullDay)` computes all the Bubble
option-set fields that the Data API doesn't auto-populate: `AMPM from/until`,
`Hour from/until`, `Minutes from/until`, `length`, `full day`, `datewasset`.
These are needed because events are written via the Data API directly, bypassing
the broken `wf/create-event` for calendar items (behavior preserved for backward
compat; see `bubble-sync-eidarix.ts` for the current path).

---

## 12. State in `ContentGateModal`

```
Static reference (from the alert row):
  bubble_action, eventPreview, libPreview, agenda_item_previews

Fetched async:
  schemaLabels         : Record<string, string> | null         (/api/schema)
  bubbleOptions        : BubbleOptionsMap                      (/api/bubble/options × 4)
  validation           : ValidationResult | "loading" | null   (/api/bubble/validate)
  docExtractionRows    : Record<string, unknown>[]             (/api/document-extractions)
  eventRecord          : BubbleRecord | null                   (/api/bubble/record)
  libRecord            : BubbleRecord | null                   (/api/bubble/record)

User-driven state:
  deletedNewIndices    : Set<number>                           — removed agenda item indices
  agendaItemEdits      : Record<number, Record<string, string>> — per-item field edits
  eventEdits           : Record<string, string>                — event field edits
  libEdits             : Record<string, string>                — library field edits

Derived (useMemo):
  derivedDate          : string   — type-based date for lib item
  libraryVectorStoreOptions : VectorStoreOption[]
  activeNewAgendaItems : AgendaItemPreview[]  — new items after edits+deletions
  activeAgendaItems    : AgendaItemPreview[]  — activeNew + existing
  displayedAgendaItems : AgendaItemPreview[]  — activeAgendaItems enriched for display
  eventFields, libFields : FieldEntry[]

Per-section control:
  sections : Record<"event"|"library_item"|"agenda_items"|"transcript"|"document", SectionState>
             where SectionState = { status: "idle"|"loading"|"done"|"error", error? }
```

---

## 13. Doc extraction enrichment (display overlay)

For PDF-backed library items, the doc extraction agent produces one row per agenda
item with (schema as of 2026-10; older rows have the names in the "legacy" column):

| Current field                            | Legacy field (older rows)                                 |
|------------------------------------------|------------------------------------------------------------|
| `agenda_item_bridgeway_title[].agenda_item_title` + `.chronicle_topics` + `.status` | `agenda_item_bridgeway_title_chronicle_topic[]` or `agenda_item_title_chronicle_topic[]` |
| `agenda_item_title_official[].official_title`                        | (unchanged)                                                |
| `agenda_item_standardized_id[].standardized_id`                      | (unchanged)                                                |
| `agenda_item_official_id[].official_id`                              | (unchanged)                                                |
| `chronicle_topic` (singular, top-level string — authoritative doc topic) | — (previously only the per-item inline `chronicle_topics` array existed) |
| `should_agenda_item_be_created` (`"Yes"` / `"No"`)                   | — (new filter; rows with `"No"` are skipped at synthesis)  |
| `date_published`, `document_description`, `document_type` (document-level) | (unchanged)                                                |

Synthesis logic (`synthesizedAgendaItems` useMemo + `enrichFromDocExtraction`)
reads the current field names first and falls back through the legacy ones for
backward compatibility with historical rows. Rows with
`should_agenda_item_be_created === "No"` are skipped entirely. When the
top-level `chronicle_topic` is present and non-N/A it supersedes the per-item
inline `chronicle_topics` array.

These rows are fetched on modal open (`/api/document-extractions?agent_call_id=X`)
and filtered to rows where `library_item_url` matches the alert row's URL. They are
used to:

1. **Synthesize** agenda items when `bubble_action.agenda_item_previews` is empty
   (page_change_agent returned N/A). Doc extraction then becomes the authoritative
   source — `isAgendaSynthesized` flag triggers patching `agenda_items: true` on
   the stored `bubble_action` so the sync executor processes them.

2. **Enrich** stored agenda items with fresher chronicle topics, official titles,
   and reference IDs via `enrichFromDocExtraction()`. Matching strategy:
   - Primary: title match (case-insensitive, after stripping status prefix)
   - Fallback: positional (sort both lists by agenda number, match by index)

3. **Supply** the library item `Date` for Publication-type items (per Section 7).

Enrichment is **display-only** — write-path payloads use the un-enriched
`bubble_action` data (so what the modal shows is what the user patched; nothing
sneaks in from a stale doc extraction cache).

---

## 14. API routes used by the Content Gate

| Route                                 | Method | Purpose |
|---------------------------------------|--------|---------|
| `/api/bubble/sync?agent_call_id=X`    | GET    | Returns the stored `bubble_action` + `bubble_sync_status` for the preview modal |
| `/api/bubble/sync`                    | POST   | Runs `syncAlert()` in-process. Body: `{ agent_call_id, action: "all"\|"agenda_items"\|"library_item"\|"event", skip_validation? }` |
| `/api/bubble/validate?agent_call_id=X` | GET   | Returns `{ valid, issues }` for pre-flight banner |
| `/api/bubble/record?agent_call_id=X&object_type=...` | GET | Fetches current Bubble record values for UPDATE sections |
| `/api/bubble/options?type=X`          | GET    | Returns `{ options: [{id, name}] }` for `organization`, `chronicletopic`, `libraryitemtype`, `calendaritemtype`. 10-min in-memory cache. |
| `/api/schema`                         | GET    | Returns `{ columns, labels }` from DynamoDB — used to render human labels in the Agent Field column |
| `/api/document-extractions?agent_call_id=X` | GET | Returns all doc extraction rows for the call; modal filters by `library_item_url` |
| `/api/alerts/patch-row`               | PATCH  | Persists field edits + stamps `bubble_library_item_id`, `bubble_event_id`, `eidarix_agenda_item_ids` after sync |
| `/api/ingest/approve-transcript`      | POST   | Approves transcript for Newsreel KB |
| `/api/ingest/approve-document`        | POST   | Approves document for Newsreel KB |

---

## 15. Edit → payload mapping

`EVENT_EDIT_MAP` and `LIB_EDIT_MAP` in `ContentGateModal.tsx` describe how each
user-facing field label maps back into `bubble_action.<preview>`:

```ts
type EditSpec = {
  previewKey?: string;    // top-level key on event_preview / library_item_preview
  fieldIdsKey?: string;   // key under preview.field_ids (consumed by the sync executor)
  isList?: boolean;       // comma-split (or array when isMulti)
  isMulti?: boolean;      // combobox-driven; values are MULTI_SEP-joined
};
```

`applyEditsToPreview(preview, edits, editMap)`:
1. For each edited field, split by `MULTI_SEP` if `isMulti` else by `,` if `isList`.
2. Write the value to `preview[previewKey]` (if set) and `preview.field_ids[fieldIdsKey]` (if set).
3. Also write the display string into `preview.fields[label]` so the modal can
   round-trip edits across re-renders.

Any field not in the edit map is treated as read-only.

---

## 16. Field reference — what each Bubble object receives

### Agenda Item (`wf/create-agenda-item/`)

| Payload key          | Source                                                            |
|----------------------|-------------------------------------------------------------------|
| `title`              | `agenda_item_previews[i].title` (stripped of status prefix)       |
| `official_title`     | `agenda_item_previews[i].official_title` (from doc extraction)    |
| `reference_id`       | `agenda_item_previews[i].reference_id` (from doc extraction)      |
| `chronicle_topics`   | Resolved Bubble topic IDs; falls back to the "empty topic" record |
| `alert_id`           | `agent_call_id`                                                   |
| `space_id`           | `CONTENT_SPACE_ID`                                                |

### Library Item (`wf/create-library-item/`)

| Payload key          | Source                                                            |
|----------------------|-------------------------------------------------------------------|
| `title`              | `library_item_preview.title`                                      |
| `url`                | `library_item_preview.url`                                        |
| `type`               | Bubble `libraryitemtype._id` resolved from `library_item_preview.type` via `LIB_TYPE_ALIASES` + versioned Data API |
| `organizations`      | Bubble `organization._id[]` resolved from org names via `matchOrgName()` (3-tier: exact → strip NAIC suffix → prefix) |
| `chronicle_topics`   | Union of library-item topics + all agenda-item topics             |
| `agenda_items`       | Bubble `agendaitem._id[]` collected in step 2 of the sync         |
| `date`               | `deriveLibraryItemDate()` → normalized to `YYYY-MM-DD`            |
| `date_display`       | Always `"Full date"`                                              |
| `summary`            | `library_item_preview.field_ids["description_text"]`              |
| `alert_id`           | `agent_call_id`                                                   |
| `space_id`           | `CONTENT_SPACE_ID`                                                |

### Calendar Event (`wf/create-event` / `wf/update-event`)

| Payload key          | CREATE                                           | UPDATE (trimmed) |
|----------------------|--------------------------------------------------|------------------|
| `id`                 | —                                                | Required         |
| `alert_id`           | `agent_call_id`                                  | `agent_call_id`  |
| `space_id`           | `CONTENT_SPACE_ID`                               | `CONTENT_SPACE_ID` |
| `chronicle_topics`   | Union with agenda items                          | Union with agenda items |
| `agenda_items`       | Bubble `agendaitem._id[]`                        | `agendaitem._id[]` |
| `agenda`             | `[bubble_library_item_id]`                       | `[bubble_library_item_id]` |
| `start_datetime`     | ISO 8601 UTC                                     | — (already set)  |
| `end_datetime`       | ISO 8601 UTC                                     | — (already set)  |
| `type`               | Bubble `calendaritemtype._id`                    | — (already set)  |
| `organizations`      | Resolved org `_id[]`                             | — (already set)  |
| `location_url`       | `event_preview.url`                              | — (already set)  |
| `phone_and_access_code` | `event_preview.call_in`                       | — (already set)  |

The UPDATE payload is intentionally trimmed to only the linking fields because
sending additional fields causes Eidarix "length isn't valid" errors on the
option-set fields (`AMPM from`, etc.).

---

## 17. Key invariants

- **Reference lookups always scope by `CONTENT_SPACE_ID`**. Lookups scoped by any
  other space return empty results, which the Eidarix workflows interpret as
  "no topics were found" / "no orgs were found".
- **The `space_id` field in workflow payloads must also be `CONTENT_SPACE_ID`** —
  the workflow resolves linked chronicle topics scoped to this field.
- **Type resolution uses the versioned URL** (`version-live/api/1.1/obj/libraryitemtype`),
  not the non-versioned URL (which returns 404 on `calendaritemtype`), and no
  space constraint (versioned URL is already space-scoped).
- **Agenda items are the first write step** because the library item and event
  payloads need their IDs. The step-by-step buttons preserve this ordering by
  blocking later steps until predecessors have completed.
- **The sync executor runs in-process** inside the dashboard ECS task. There is no
  Lambda intermediary; the previously-deployed `web-change-tracker-prod-bubble-sync`
  Lambda is unused by the current code path (its Terraform resource remains in sync
  with the executor's `EIDARIX_VERSION` as a historical mirror).
- **Enrichment is display-only.** Write-path payloads use `bubble_action`,
  possibly patched by the user via `applyEditsToPreview`. Doc extraction data
  flows into the write path only when the user explicitly accepts a doc rerun.
- **Chronicle topics flow upward.** Agenda-item topics → library item topics →
  event topics via `allLibraryItemTopics()`. This guarantees the event in Bubble
  carries every topic from every linked agenda item.
- **Agenda-topic source precedence (sync executor).** `getAgendaTopicNames(row)`
  reads chronicle-topic names from `bubble_action.agenda_item_previews` first
  (the modal patches these with doc-extraction-synthesized items before firing
  the sync) and falls back to the alert row's `agenda_item_title_chronicle_topics`
  field only when previews are empty. Needed because the page-change agent
  often leaves `agenda_item_title_chronicle_topics` as `"N/A"` placeholders
  while the doc extraction agent produces the real topic lists per agenda item.
- **UPDATE display vs. send parity.** `mergeWithCurrentRecord` in the modal
  does **not** replace the computed preview value for list-replacing fields
  (`Chronicle Topics`, `Linked Agenda Items`) with the Bubble record's current
  list, because the sync executor PATCH overwrites those Bubble lists
  wholesale. `Organizations` is NOT in that set — the current executor does
  not update it on either lib or event UPDATE, so showing Bubble's current
  value there is correct.
- **UPDATE "always updated" fields.** Independent of what the classifier
  marks in `event_preview.fields` / `library_item_preview.fields`, the modal
  forces these labels into the changed-set so they amber-highlight on
  UPDATE and show the preview-computed values the sync will send:
  - Event UPDATE: `Chronicle Topics` (`Topics - dt`), `Linked Agenda Items`
    (`attached agenda items`), `Agenda` (library item link)
  - Library UPDATE: `Chronicle Topics` (`Topics - dt`)
  Everything else on an UPDATE card shows the authoritative Bubble value
  from `/api/bubble/record`.
- **Ref # written via Data API PATCH.** The `wf/create-agenda-item/` workflow
  does not accept a parameter for the NAIC official ID — it writes `BA Ref #`
  from `reference_id` and silently copies that into `Ref #`. The executor
  follows every successful workflow create with a PATCH
  `/obj/agendaitem/{id}` body `{"Ref #": officialId}` to layer the correct
  NAIC ID on top. Non-fatal; a failed PATCH only logs a warning.
