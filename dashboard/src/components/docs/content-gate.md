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
| Agent Field   | Small color-coded source badge (**Web Extraction** / **Document Extraction** / **Derived** / **Transcript**) on its own line, with the human-readable label from the agent's output schema (`/api/schema`) stacked underneath. Falls back to a hand-maintained map in `AGENT_FIELD_LABEL_FALLBACKS` for internal/derived fields. The vertical stack prevents the long badge text from forcing awkward mid-word wraps of the field name. |
| Bubble Field  | Name of the Bubble field being written (e.g., "Name", "Date"). |
| Value         | Either a text input (free-text), a `SearchableCombobox` (dropdown), chronicle-topic chips, a numbered list, or read-only text. |

The source badge is driven by a `source: FieldSource` property on each
`FieldEntry`. The `SOURCE_BADGE` map in `ContentGateModal.tsx` controls the
label, hover title, and color class per source. Classifier-derived fields (e.g.,
the library item `Type` which is mapped from the alert's `alert_type`) are
badged **Web Extraction** because the classifier operates downstream of Web Extraction
output.

For UPDATE sections, when the modal has fetched the current Bubble record,
unchanged fields display the authoritative Bubble value (`mergeWithCurrentRecord`),
while changed fields show the proposed new value highlighted amber. The modal
forces a set of "always updated" labels (Chronicle Topics, Linked Agenda Items,
Agenda for events; Chronicle Topics for library items) into the changed set so
users see the preview values the sync will PATCH, independent of whether the
classifier flagged them.

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
│ New Library Item                                ✓ Done   │  ← header title
│ <content title shown as clickable indigo link ↗>         │  ← subtitle
├──────────────────────────────────────────────────────────┤
│ [3-column field table]                                   │
├──────────────────────────────────────────────────────────┤
│          [Add to Library Vector Store ▼] [Create in Bubble] │ ← footer
└──────────────────────────────────────────────────────────┘
```

- **Header title** = `{Action Noun} {Object Type}` — one clean bold line.
  - For Bubble sections: `New Library Item` / `Update Library Item` /
    `New Event` / `Update Event`.
  - For Newsreel sections: `Publish Meeting Transcript` /
    `Publish Library Document`.
  - The action noun is a label for *what the card is*, not what the button
    does — the button verb ("Create in Bubble" / "Update in Bubble" /
    "Publish to Newsreel") lives at the bottom.
- **Border + background color** follow the action: green for new, blue for
  update, purple for publish.
- **Subtitle** = the content's title (e.g., library item name, event name),
  rendered as a clickable indigo link opening in a new tab when a URL is
  available (library item → `library_item_url`, document → `library_item_url`,
  event → `event_url`). Falls back to plain gray text otherwise (e.g.,
  transcript filename). Driven by `subtitleHref?: string` on `SectionProps`.

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

## 8. Vector store (Library Vector Store) consolidation

Documents and transcripts flow to any curated knowledge-base namespace via the
same `/internal/documents/ingest` API (contract: `docs/integration/newsreel-ingest-api.md`).
When a library item is already being written to Bubble, the vector-store action
is folded into the Library Item card's footer as a secondary split-button:

```
[Add to Library Vector Store ▼]   [Create in Bubble]
    ├─ Newsreel Generation          (default / primary — mirrors `ingest_status: "approved"`)
    ├─ NAIC Proceedings
    ├─ Bridgeway SharePoint
    ├─ ART State System
    ├─ Chronicles (ba:*)             ⚠ no dedupe against Bubble-synced content
    ├─ Newsreels (ba:*)              ⚠ "
    ├─ NAIC Guidelines (ba:*)        ⚠ "
    ├─ International Guidelines (ba:*)  ⚠ "
    └─ Rating Agencies (ba:*)        ⚠ "
```

- `AddToVectorStoreButton` renders the dropdown; `VECTOR_STORE_DESTINATIONS`
  in `ContentGateModal.tsx` enumerates the full set with human labels and a
  per-destination `disabledReason` tooltip for the `ba:*` dedup caveat.
- Clicking a destination calls `handlePublishTranscript(namespace)` or
  `handlePublishDocument(namespace)` depending on whether the row has a
  transcript (`transcript_s3_key` + `alert_type === "New Meeting Transcript Available"`)
  or a document URL. Both route to `POST /api/ingest/approve-{transcript,document}`
  with `{ namespace }` in the body.
- The ingest routes validate `namespace` against an `ALLOWED_NAMESPACES` set
  mirroring the upstream contract; anything outside returns 400 locally.
- Per-destination state lives in `kbIngestState: Record<namespace, SectionState>`
  on the modal. Each dropdown item renders its own `✓ Added` / `Working…` /
  `Failed` indicator — the user can send the same content to multiple KBs
  independently.
- The row-level `ingest_status: "approved"` flag only tracks the primary
  `newsreel-generation:ART` destination so the Alerts-table "✓ Complete" badge
  keeps its original meaning. On modal open, `kbIngestState` is seeded with
  `{"newsreel-generation:ART": { status: "done" }}` when the row's
  `ingest_status === "approved"`.

If there's no library item action to piggy-back on (rare), a standalone section
with just the vector-store dropdown is rendered.

---

## 9. Agenda item editing

New and existing agenda items share one unified "N Agenda Items" card (header
text is neutral — each mini-card carries its own `NEW` / `EXISTING` / `UPDATED`
status badge). Each item renders as a bordered mini-card with the same
3-column field table used by the library item / event sections:

| Agent Field                 | Bubble Field | Value                                  |
|-----------------------------|--------------|----------------------------------------|
| Agenda Item Bridgeway Title | BA title     | editable text (New) / read-only (Existing) |
| Agenda Item Official Title  | NAIC Title   | editable text (New) / read-only (Existing) |
| Agenda Item Standardized ID | BA Ref #     | editable text (New) / read-only (Existing) |
| Agenda Item Official ID     | Ref #        | editable text (New) / read-only (Existing) |
| Agenda Item Chronicle Topics | Topics - dt | searchable multi-select (New) / chips (Existing) |

- All five fields auto-populate from the matching doc-extraction row
  (`agenda_item_bridgeway_title[]`, `agenda_item_title_official[]`,
  `agenda_item_standardized_id[]`, `agenda_item_official_id[]`,
  `chronicle_topic` top-level scalar).
- **New** items are editable and will be POSTed to `wf/create-agenda-item/`.
- **Existing / Updated** items are read-only (editing wouldn't affect the
  Bubble record) but still carry a × delete button so the user can drop them
  from the final link set.
- `×` on a New item removes it from the create batch (`deletedNewIndices`).
  `×` on an Existing item removes it from the link set
  (`deletedExistingIndices`).
- Per-item edits land in `agendaItemEdits: Record<number, Record<string, string>>`
  and are applied via `applyAgendaEdits(item, i)` before patching
  `bubble_action.agenda_item_previews`.

**Ref # Data-API PATCH**: the `wf/create-agenda-item/` workflow does not accept
an input parameter for `Ref #`, so the executor PATCHes
`/obj/agendaitem/{id}` with `{"Ref #": officialId}` immediately after each
successful workflow create. Non-fatal — a failed PATCH only logs a warning.

**Status prefixes** (`"New - "`, `"Existing - "`, `"Updated - "`) are stripped
at display time via `stripStatusPrefix()` and also before any Bubble API write.
`N/A` and missing status values on synthesized items are normalized to `"New"`
so the dependency guard fires correctly.

**Existing/Updated items** (status != `"New"`) are not re-created — they are
looked up in Bubble by title via `findAgendaItemByTitle()` (searches for the
"title" or "BA title" field). If the item can't be found, the whole sync fails
with a `RuntimeError` rather than silently proceeding.

---

## 10. Pre-flight validation

`validateSyncPlan(agentCallId, rows)` in `bubble-sync-eidarix.ts` is called at
modal open via `GET /api/bubble/validate`. It runs four parallel lookups:

| Check                      | Severity | Result                                                   |
|----------------------------|----------|----------------------------------------------------------|
| Unresolved org names       | error    | Record will publish with empty Organizations field       |
| Unresolved chronicle topics | warning  | "Calendar Events with no Topic" fallback will be used (surfaced per-agenda-item, not in the banner) |
| Library item type not found | error   | Library item will publish with no type                   |
| Calendar item type not found | error  | Event will publish with no type                          |

Issues render in the `ValidationBanner` at the top of the modal, **except**
chronicle-topic warnings. Those are surfaced inline on the agenda-item mini-card
they belong to (amber strip under the card header listing only that item's
unresolved topics) so the user can see which agenda item is affected without
cross-referencing. The banner suppresses `issues.filter(i => i.field ===
"Chronicle Topics")`. The per-item check runs client-side against the
`bubbleOptions.chronicletopic` list that's already loaded for the dropdowns
(exact case-insensitive match on `.name`); it's a no-op until the fetch
resolves.

Users can still confirm and publish — the warnings are informational, not
blocking.

**Note on `chronicle_topic` scalar:** the doc-extraction schema defines the
top-level `chronicle_topic` as a single string, but the agent sometimes emits
`;`- or `|`-joined values. `splitChronicleTopic()` (both in the modal and in
`bubble-sync-eidarix.ts::splitTopicNames`) splits on those two delimiters only —
comma is NOT a delimiter because real Bubble topic titles contain commas
(e.g. `"Markets, Risk-Based Capital, and Reserves"`). Each piece then resolves
against Bubble independently.

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

### Pre- and post-write snapshots

Bubble's Data API exposes no record-version endpoint — once a PATCH lands, the
prior field values are unrecoverable unless we captured them out of band. The
executor uses `snapshotBubbleRecord(type, id, base?, phase?)` to GET the full
record body and log it to CloudWatch, both **before** and **after** every
PATCH against an existing record:

- Before PATCH: logged at INFO under tag `[bubble-sync][SNAPSHOT-pre]` with
  `pre-state={…}` on the same line.
- After successful PATCH: logged at INFO under tag `[bubble-sync][SNAPSHOT-post]`
  with `post-state={…}` on the same line.

The pre/post pair gives operators a verifiable before/after audit trail for
every executor-driven write, and lets a reverter PATCH the pre-state back
field-for-field if a sync needs to be undone.

Non-fatal: a failed snapshot warns and returns `null`; the PATCH still
proceeds. CREATE paths and the post-create Ref# PATCH on freshly made agenda
items are intentionally not snapshotted — the record didn't exist a
millisecond earlier so there's nothing to preserve.

Hooked at three PATCH sites in `bubble-sync-eidarix.ts` (pre- and post- at
each):

| PATCH target | Scenario |
|---|---|
| `calendaritem/<id>` | Event UPDATE (trimmed PATCH of `Topics - dt`, `Agenda`, `attached agenda items`, `Eiderix Ref`) |
| `libraryitem/<id>` | Library item UPDATE — PATCH `Topics - dt` |
| `agendaitem/<id>` | Agenda topic refresh when `eidarix_agenda_item_ids` already stamped |

**Recovery recipe**:

```bash
# Find the pre- and post- pair for a specific record around a given time
AWS_PROFILE=bridgeway aws logs tail /ecs/naic-dashboard --since 24h \
  | grep -E "SNAPSHOT-(pre|post).*<type>/<bubble_id>"
```

Everything after `pre-state=` on the `SNAPSHOT-pre` line is the JSON body
that was live immediately before the subsequent PATCH; everything after
`post-state=` on the matching `SNAPSHOT-post` line is the body immediately
after. CloudWatch retention on `/ecs/naic-dashboard` is ~30 days.

**Out-of-band CLI writes: use `scripts/bubble_safe_write.py`**

Operator-driven `curl -X PATCH` / `curl -X DELETE` against the Bubble Data
API bypasses the executor snapshot — this is the gap the Nov 10 incident
exposed. The safe-write wrapper closes that gap by persisting pre- and
post-state snapshots to S3 before any ad-hoc write:

```bash
# Inspect (snapshot + print, no write)
AWS_PROFILE=bridgeway python scripts/bubble_safe_write.py \
  --type calendaritem --id 1789598655253x668569308291924000 --inspect

# PATCH — snapshots pre, applies PATCH, snapshots post
AWS_PROFILE=bridgeway python scripts/bubble_safe_write.py \
  --type calendaritem --id 1789598655253x668569308291924000 \
  --patch '{"Topics - dt": ["<topic_id>"]}'

# DELETE — snapshots pre, then DELETEs (no post; record is gone)
AWS_PROFILE=bridgeway python scripts/bubble_safe_write.py \
  --type agendaitem --id 1780749123275x739856002318684200 --delete
```

Snapshots land in `s3://<CHANGELOG_BUCKET>/snapshots/bubble/<type>/<id>/<utc-iso>_<phase>.json`.
The script fetches the Bubble API key from env (`BUBBLE_API_KEY`) or SSM
(`/naic-dashboard/prod/bubble_api_key`) with the same precedence as the
executor. **Operators: never `curl -X PATCH` or `curl -X DELETE` a Bubble
record without using this wrapper (or equivalent pre-state capture).**

### Observability — structured event metrics

`bubble-sync-eidarix.ts` emits `[METRIC] <event> {…json…}` lines at the
regression-prone paths — one per event so CloudWatch Log metric filters
can count them without parsing freeform strings. Events currently
instrumented:

| Event | When it fires | Why it matters |
|---|---|---|
| `sync_error` | Top-level `catch` in `syncAlert()` | Any > 0 is unusual — unhandled exception. |
| `patch_failed` | Bubble Data API PATCH returned non-OK (`libraryitem`, `agendaitem`) | Should never happen in steady state. |
| `agenda_item_not_found` | `findAgendaItemByTitle()` miss for an Existing/Updated preview | Classifier marked the item as existing but Bubble can't find it by title. |
| `patch_fallback_to_create` | UPDATE `match_search` returned nothing — fell back to CREATE | URL/title drift between classification and sync. |
| `topic_fallback_used` | Agenda item create path fell onto the empty-topic chronicletopic | Lookup failed for all provided topic names. |
| `topic_not_found` | A topic name failed all five lookup probes | Agent/Bubble drift — covered more fully by the modal warning strip. |
| `org_not_found` | Org name failed `matchOrgName` | Classifier had an org the Bubble org table doesn't know. |

Terraform in `dashboard/infra/terraform/cloudwatch.tf` provisions one
`aws_cloudwatch_log_metric_filter` per event, counting hits into namespace
`NaicDashboard/BubbleSync`. View in the AWS console under CloudWatch →
Metrics → NaicDashboard/BubbleSync. Attach alarms once baselines settle
(the Pipeline-side pattern in `infra/terraform/cloudwatch.tf` wires SNS
email alerts and is a template for adding alarms here).

Operator recipe — eyeball the current rate:

```bash
AWS_PROFILE=bridgeway aws cloudwatch get-metric-statistics \
  --namespace NaicDashboard/BubbleSync \
  --metric-name patch_fallback_to_create \
  --start-time $(date -u -v-1H +%Y-%m-%dT%H:%M:%SZ) \
  --end-time $(date -u +%Y-%m-%dT%H:%M:%SZ) \
  --period 3600 --statistics Sum
```

### Topic name matching

Agent-emitted chronicle topic names drift from Bubble's canonical `Title` in
several ways. `lookupTopicWithArticle(nameMap, rawName)` applies six probes
in order; each uses `normalizeTopicKey` (collapses any Unicode whitespace —
including U+00A0 NBSP — to a single ASCII space, lowercases, trims) and each
is uniqueness-gated so ambiguous drift falls through to the "not found"
warning rather than silently matching the wrong topic:

1. **Exact normalized.** `"Foo Bar"` ↔ `"Foo Bar"`, modulo invisible
   whitespace differences (fixes the NBSP class of mismatch where Bubble
   Titles like `"ALM Derivatives & Derivative Investments"` carry an
   embedded NBSP).
2. **Leading-article toggle.** `"The Foo"` ↔ `"Foo"` in either direction.
   Catches agent-prepended or agent-dropped `"The "` (e.g. agent emits
   `"The U.S. Department of the Treasury…"` but Bubble stores it without
   the article; or agent emits `"NAIC Investment Oversight Framework"` and
   Bubble has `"The NAIC Investment Oversight Framework"`).
3. **Unique prefix with separator (agent-truncated).** If exactly one Bubble
   key equals `base + <sep> + <suffix>` for a conjunction/punctuation
   separator (`" &"`, `" and "`, `" – "`, `" — "`, `" - "`, `", "`, `": "`),
   accept it. Catches agent truncation like `"Liquidity Stress Tests (LSTs)"`
   → Bubble `"Liquidity Stress Tests (LSTs) & Macroprudential Risks"`.
4. **Unique word-boundary suffix.** If the agent name is multi-word (contains
   a space, so single-word matches like bare `"Risk"` are refused) and
   exactly one Bubble key ends with `" " + base`, accept it. Catches
   agent-dropped leading qualifier words like `"RBC Covariance & Asset
   Concentration Risk"` → Bubble `"Life RBC Covariance & Asset Concentration
   Risk"`.
5. **Unique agent-superset-of-Bubble (reverse of probe 3).** If exactly one
   Bubble key `k` satisfies `base.startsWith(k + <sep>)` for one of the
   same separators as probe 3, accept it. Catches agent *extension* like
   `"ALM Derivatives & Derivative Wrapped Investments"` → Bubble
   `"ALM Derivatives"`. Uniqueness-gated so a Bubble topic that is itself
   a prefix of multiple other Bubble topics still falls through.
6. **Not found.** Modal shows the per-mini-card warning strip
   `"1 chronicle topic not found — Calendar Events with no Topic fallback
   will be used"`; executor writes the `empty topic = true` chronicletopic
   record ID.

`canonicalizeTopics(topics)` in the modal uses the same probe chain to
upgrade each agent-emitted topic string to its canonical Bubble Title
before passing to the `SearchableCombobox`, so the dropdown pre-checks
the right row and the displayed chip is the real Bubble spelling.

Mirrored helpers:
- Server-side: `lookupTopicWithArticle` in `src/lib/bubble-sync-eidarix.ts`,
  wired at `resolveTopicIds` (write path), `validateSyncPlan` (pre-flight
  warning), and the agenda-item-create topic resolution.
- Client-side: duplicated in `src/components/ContentGateModal.tsx` because
  the executor reads `process.env` and must stay server-only; shared
  normalization semantics though.

### "Preliminary — <X>" alert type normalization

The web-tracking agent emits `"Preliminary — <X>"`, `"Preliminary - <X>"`
(em-dash + hyphen variants), and bare `"Preliminary"` for still-settling
announcements. These carry the same payload shape as their underlying
type, so the classifier (`bubble/bubble_sync_classifier.py`) and
`document_agent.should_run_for_alert()` strip the prefix via
`_strip_preliminary()` before every dictionary lookup (`_TYPE_MAP`,
`_LIBRARY_ITEM_TYPE`, `_LIB_UPDATE_CHANGES`, `_EVENT_AGENDA_LINK_LABEL`,
`DOCUMENT_ALERT_TYPES`). The row **still stores** the full original
`alert_type`; only the dictionary lookups use the stripped form. Bare
`"Preliminary"` defaults to `"New Materials"` so it still classifies.
Backfill via `scripts/backfill_bubble_action.py --force` will pick up
any historical Preliminary rows.

### Current flat-schema agenda-item field

`_build_agenda_previews()` and the agenda-flag override in `classify_alert()`
read `alert.get("agenda_item_title")` first, falling back to the historical
`agenda_item_title_chronicle_topics` and `agenda_item_title_and_chronicle_topics`.
Any time the DynamoDB schema's agenda-item field name is renamed, the current
name must be added to the head of that fallback chain — the classifier does
not read the schema directly.

### Title overlay from document-extraction agent

`enrich_with_doc_extraction()` (same module) overlays
`library_item_preview.title`, `fields["Name"]`, and `field_ids[LI.NAME]`
from the doc-extraction row's `library_item_title` (fallback
`document_title`) whenever a matching extraction exists. Applies to BOTH
CREATE and UPDATE paths. Intentionally does **not** touch
`match_search.title` — that's keyed on what Bubble already stores for the
UPDATE lookup and rewriting it would break the record match.

The modal's **Name** field (and the vector-store section's **Title** field)
compute the source badge dynamically from `docExtractionRows`: if any
matching extraction row has a non-N/A `library_item_title` /
`document_title`, badge the field as `doc_extraction` with
`agentField = "library_item_title"`. Otherwise fall back to
`web_tracking` + `library_item_preliminary_title.title`. Keeps the
badge honest about which agent's output the user is looking at.

### Type overlay from document-extraction agent

Same overlay pattern as title: `enrich_with_doc_extraction()` overlays
`library_item_preview.type` (and `fields["Type"]`, `fields["Doc Type"]`)
from the doc-extraction row's `document_type` field whenever present.
Applies to BOTH CREATE and UPDATE paths. The modal's Type-field source
badge reflects this: if a matching doc extraction row has a non-N/A
`document_type`, the badge reads **Document Extraction** +
`agentField = "document_type"`. Otherwise it falls back to **Web
Extraction** + `agentField = "alert_type (classifier)"`.

Why overlay rather than keep the classifier's `_LIBRARY_ITEM_TYPE[alert_type]`
lookup: the classifier lookup is driven by regex matching on the alert_type
string and falls over for any variant not hardcoded in the map. The ALM
Derivatives bug on 2026-11-19 is the canonical example — alert_type
`"Preliminary. Updated Request for Comment"` (period separator) wasn't
being normalised correctly, so the lookup fell to the `"Document"` default,
which the executor's `LIB_TYPE_ALIASES` then mapped to `"Publication"`
instead of the correct `"Proposed Guidance & Support Materials"`. The doc
agent actually reads the PDF and knows the real document type — use that.

### Library item type resolution + inline warning

`resolveLibraryItemType(typeName)` in the executor returns a `{id,
canonical, matchedAs}` tuple with four possible outcomes:

| `matchedAs` | Meaning | Validator surfaces |
|---|---|---|
| `"exact"` | Bubble has a `libraryitemtype` with exactly this `Title` | no issue |
| `"alias"` | The lowercased typeName is in `LIB_TYPE_ALIASES` and that alias maps to a Bubble `Title` | **warning** |
| `"fuzzy"` | Token-overlap scoring finds a Bubble `Title` sharing ≥70% of word tokens (safety net for unseen typos the alias table doesn't cover) | **warning** |
| `null` | Nothing matched | **error** |

`validateSyncPlan()` returns the alias / fuzzy outcome as a **warning**
with `canonical` set to the Bubble title the publish will actually use.
The modal:
1. Filters `Library Item Type` issues OUT of the top `ValidationBanner`
   (same treatment as Chronicle Topics — surfaced inline on the card
   they belong to).
2. Reads the issue's `canonical` and overlays the library item
   preview's `type` with it (`effectiveLibPreview`), so the Type
   combobox and the field table display the actual Bubble title that
   will land, not the agent's typo.
3. Renders a warning strip at the top of the Library Item card showing
   the agent's original value and the fallback:
   `⚠ Type: Type "Proposed Guidelines & Support Materials" not an exact
   Bubble match — will fall back to "Proposed Guidance & Support
   Materials" (alias)`

Safety net at the executor layer: `LIB_TYPE_ALIASES` currently covers
the common agent misspelling `"Proposed Guidelines & Support Materials"`
(plural "Guidelines", with an L) → `"Proposed Guidance & Support
Materials"` (the canonical Bubble type title) and the legacy fallbacks
(`"agenda"` → `"Agenda & Materials"`, `"document"` → `"Publication"`,
etc.). New typos the alias table misses are caught by the fuzzy probe.

### Preliminary alert type stripping

`_strip_preliminary()` in the classifier normalises
`"Preliminary <sep> <X>"` / `"<X> <sep> Preliminary"` / bare `"Preliminary"`
to the underlying type before every map lookup (`_TYPE_MAP`,
`_LIBRARY_ITEM_TYPE`, `_LIB_UPDATE_CHANGES`, `_EVENT_AGENDA_LINK_LABEL`).
`_strip_preliminary_doc()` in `document_agent` mirrors this so
`DOCUMENT_ALERT_TYPES` covers Preliminary variants.

Separator class: em-dash (`—`), en-dash (`–`), hyphen (`-`), period (`.`),
colon (`:`) — every punctuation we've seen the agent insert between
"Preliminary" and the underlying type. Bare `"Preliminary"` after strip
defaults to `"New Materials"`. The row still stores the full original
`alert_type`; only the dictionary lookups use the stripped form.

### Calendar event canonical title

Freshly-created calendar events (whether the classifier originally said CREATE,
or UPDATE that fell back to CREATE via the match_search miss path) have their
title built by `buildCanonicalEventTitle()` to match the pattern observed on
existing Bubble events:

```
<Org Short Name> | <topics joined by `;`> |  <Meeting|Last comment date>
```

- **`<Org Short Name>`** comes from the Bubble `organization` record's
  `Short Name` field. Already includes the `"NAIC "` prefix where applicable
  (e.g. `"NAIC SAPWG"`, `"NAIC BWG"`, `"NAIC EX-Committee"`). We look up the
  org via `match_search.org` → full `Name` → `Short Name`. Falls back to the
  full Name when the Short Name field is empty or the org isn't found.
- **topics segment** is the union of event-preview topics + agenda-item
  topics, joined by a bare `;` (no surrounding space). Defaults to
  `"Relevant Topics TBD"` when the agent produced no topics.
- **final segment** is `"Last comment date"` when `alert_type` (after
  `_strip_preliminary`) is in `_RFC_ALERT_TYPES` (`New Request for
  Comment`, `Updated Request for Comment`, `New Effective Date`,
  `Updated Effective Date`). Otherwise `"Meeting"`.
- Note the **double space** before the final segment (` |  Meeting`) —
  matches existing Bubble events exactly.

When we can't resolve a Short Name (missing org, Bubble lookup failure,
etc.) the executor falls back to the classifier's `ep.title` so publishing
never fails outright on title construction.

**UI surfacing.** `validateSyncPlan()` returns `effectiveEventTitle` in
the `ValidationResult`, so the modal displays the canonical title the
executor will actually write — the user never sees the classifier's
old-format title when a new event is about to be created. For UPDATE
paths the trimmed PATCH doesn't touch the title; the modal keeps showing
the live Bubble record's title via `currentRecord` (fetched from
`/api/bubble/record`), so there's no misleading preview of a title that
won't change.

### `/api/bubble/record` org filter

The calendar record lookup endpoint mirrors the executor's `findCalendarItem`
constraints exactly: `space`, `date` range, **and** `orgs contains org_id`
where `org_id` is resolved from `match_search.org`. Without the org filter,
ANY event in the content space on the target date returns a false positive
and the modal reports `found: true`, suppressing the `eventFallbackToCreate`
→ `fallbackNotice` chain. Users then see "Update event" when CREATE is what
will actually happen at sync time.

### Sibling-row stamp scoping

Sibling rows share `agent_call_id` but have distinct `library_item_url`
(the `storage/alert_s3.py` fan-out fix in `5fb4b4e` makes each row carry
its own URL + per-row `bubble_action.library_item_preview.url`). The
executor's `patchRow(rows, agentCallId, fields, libraryItemUrl?)` helper
scopes stamp writes by `library_item_url` so publishing one sibling's
library item only stamps `bubble_library_item_id` / `bubble_sync_status`
on that single sibling row. Without this, publishing one doc would mark
every sibling "✓ Complete" in the modal. All three `patchRow` call sites
(agenda_items action, main sync completion, error path) pass the
executor's resolved `libraryItemUrl` so the scoping is consistent.

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
| `chronicle_topic` (top-level string, schema-typed as scalar but may hold `;`/`|`-joined values — authoritative doc topic) | — (previously only the per-item inline `chronicle_topics` array existed) |
| `should_agenda_item_be_created` (`"Yes"` / `"No"`)                   | — (new filter; rows with `"No"` are skipped at synthesis)  |
| `date_published`, `document_description`, `document_type` (document-level) | (unchanged)                                                |

Synthesis logic (`synthesizedAgendaItems` useMemo + `enrichFromDocExtraction`)
reads the current field names first and falls back through the legacy ones for
backward compatibility with historical rows. Rows with
`should_agenda_item_be_created === "No"` are skipped entirely. When the
top-level `chronicle_topic` is present and non-N/A it supersedes the per-item
inline `chronicle_topics` array — passed through `splitChronicleTopic()` so a
`;`/`|`-joined value yields multiple distinct topics.

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
  the sync). If previews are empty it falls back to the alert row's agent-output
  fields, preferring the current field name `agenda_item_title[]` and falling
  back to the legacy `agenda_item_title_chronicle_topics[]` for historical rows.
  It then folds in the row's top-level `chronicle_topic` scalar (new in the
  2026-10 Web Agent schema) when it's non-N/A and not already in the list,
  running the value through `splitTopicNames()` so a `;`/`|`-joined scalar
  contributes multiple distinct topics.
- **Doc-extraction URL filter is forgiving.** The modal filters doc extraction
  rows to the alert's `library_item_url` with three passes: (1) exact match,
  (2) normalized match (lowercase, strip trailing slash, strip fragment), and
  (3) if all doc rows share the same URL anyway, use all of them (one-PDF
  case where the stored URL differs only cosmetically from the alert URL).
  Falls through to logging rather than crashing when nothing matches.
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
